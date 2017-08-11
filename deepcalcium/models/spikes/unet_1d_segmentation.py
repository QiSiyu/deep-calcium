from __future__ import division, print_function
from glob import glob
from itertools import cycle
from keras.callbacks import Callback, ModelCheckpoint, CSVLogger, ReduceLROnPlateau, EarlyStopping
from keras.optimizers import SGD, Adam
from keras.losses import binary_crossentropy
from math import ceil
from os import path, mkdir
from skimage.morphology import local_maxima
from time import time
import h5py
import keras.backend as K
import logging
import numpy as np
import os

from deepcalcium.utils.runtime import funcname
from deepcalcium.utils.config import CHECKPOINTS_DIR
from deepcalcium.utils.keras_helpers import MetricsPlotCallback, load_model_with_new_input_shape
from deepcalcium.utils.spikes import F2, prec, reca, ytspks, ypspks, weighted_binary_crossentropy, plot_traces_spikes

rng = np.random
MODEL_URL_LATEST = 'https://github.com/alexklibisz/deep-calcium/releases/download/v0.0.1-weights/unet1d_model.hdf5'


class _SamplePlotCallback(Callback):
    """Keras callback that plots sample predictions during training."""

    def __init__(self, save_path, traces, spikes, nb_plot=30, title='Epoch {epoch:d} loss={loss:.3f}'):

        self.save_path = save_path
        self.traces = traces
        self.spikes = spikes
        self.nb_plot = min(len(traces), nb_plot)
        self.title = title

    def on_epoch_end(self, epoch, logs):

        # Get newest weights, predict, plot.
        spikes_pred = self.model.predict(self.traces[:self.nb_plot])
        plot_traces_spikes(traces=self.traces[:self.nb_plot],
                           spikes_true=self.spikes[:self.nb_plot],
                           spikes_pred=spikes_pred[:self.nb_plot],
                           title=self.title.format(epoch=epoch, **logs),
                           save_path=self.save_path.format(epoch=epoch),
                           dpi=120)


def unet1d(window_shape=(128,), nb_filters_base=32, conv_kernel_init='he_normal', prop_dropout_base=0.05, margin=4):
    """Builds and returns the UNet architecture using Keras.

    # Arguments
        window_shape: tuple of one integer defining the input/output window shape.
        nb_filters_base: number of convolutional filters used at the first layer. This is doubled
            after every pooling layer, four times until the bottleneck layer, and then it gets
            divided by two four times to the output layer.
        conv_kernel_init: weight initialization for the convolutional kernels. He initialization
            is considered best-practice when using ReLU activations, as is the case in this network.
        prop_dropout_base: proportion of dropout after the first pooling layer. Two-times the
            proportion is used after subsequent pooling layers on the downward pass.
        margin: the effective "error margin" for the architecture. A margin of 2 means that a predicted
            positive will at least be partially correct if it falls within 2 time-steps of the ground-truth
            positive. This is implemented by max-pooling before the final softmax layer.
    # Returns
        model: Keras model, not compiled.
    """

    from keras.layers import Input, Conv1D, MaxPooling1D, Dropout, concatenate, BatchNormalization, Lambda, UpSampling1D, Activation
    from keras.models import Model

    drp = prop_dropout_base
    nfb = nb_filters_base
    cki = conv_kernel_init

    # Theano vs. TF setup.
    assert K.backend() == 'tensorflow', 'Theano implementation is incomplete.'

    def up_layer(nb_filters, x):
        return UpSampling1D()(x)

    def conv_layer(nbf, x):
        x = Conv1D(nbf, 5, strides=1, padding='same', kernel_initializer=cki)(x)
        x = BatchNormalization(axis=-1)(x)
        return Activation('relu')(x)

    x = inputs = Input(window_shape)
    x = Lambda(lambda x: K.expand_dims(x, axis=-1))(x)

    x = conv_layer(nfb, x)
    x = conv_layer(nfb, x)
    dc_0_out = x

    x = MaxPooling1D(2, strides=2)(x)
    x = conv_layer(nfb * 2, x)
    x = conv_layer(nfb * 2, x)
    x = Dropout(drp)(x)
    dc_1_out = x

    x = MaxPooling1D(2, strides=2)(x)
    x = conv_layer(nfb * 4, x)
    x = conv_layer(nfb * 4, x)
    x = Dropout(drp * 2)(x)
    dc_2_out = x

    x = MaxPooling1D(2, strides=2)(x)
    x = conv_layer(nfb * 8, x)
    x = conv_layer(nfb * 8, x)
    x = Dropout(drp * 2)(x)
    dc_3_out = x

    x = MaxPooling1D(2, strides=2)(x)
    x = conv_layer(nfb * 16, x)
    x = conv_layer(nfb * 16, x)
    x = up_layer(nfb * 8, x)
    x = Dropout(drp * 2)(x)

    x = concatenate([x, dc_3_out], axis=-1)
    x = conv_layer(nfb * 8, x)
    x = conv_layer(nfb * 8, x)
    x = up_layer(nfb * 4, x)
    x = Dropout(drp * 2)(x)

    x = concatenate([x, dc_2_out], axis=-1)
    x = conv_layer(nfb * 4, x)
    x = conv_layer(nfb * 4, x)
    x = up_layer(nfb * 2, x)
    x = Dropout(drp * 2)(x)

    x = concatenate([x, dc_1_out], axis=-1)
    x = conv_layer(nfb * 2, x)
    x = conv_layer(nfb * 2, x)
    x = up_layer(nfb, x)
    x = Dropout(drp)(x)

    x = concatenate([x, dc_0_out], axis=-1)
    x = conv_layer(nfb, x)
    x = conv_layer(nfb, x)

    x = Conv1D(2, 1)(x)
    x = MaxPooling1D(margin + 1, strides=1, padding='same')(x)
    x = Activation('softmax')(x)

    #x = Lambda(lambda x: x[:, :, 1:])(x)
    #x = MaxPooling1D(margin + 1, strides=1, padding='same')(x)
    x = Lambda(lambda x: x[:, :, -1])(x)
    model = Model(inputs=inputs, outputs=x)

    return model


def get_dataset_attrs(dspath):
    fp = h5py.File(dspath)
    attrs = {k: v for k, v in fp.attrs.items()}
    fp.close()
    return attrs


def get_dataset_traces(dspath):
    fp = h5py.File(dspath)
    traces = fp.get('traces')[...]
    fp.close()
    m = np.mean(traces, axis=1, keepdims=True)
    s = np.std(traces, axis=1, keepdims=True)
    traces = (traces - m) / s
    assert -5 < np.mean(traces) < 5, np.mean(traces)
    assert -5 < np.std(traces) < 5, np.std(traces)
    return traces


def get_dataset_spikes(dspath):
    fp = h5py.File(dspath)
    spikes = fp.get('spikes')[...]
    fp.close()
    return spikes


class UNet1DSegmentation(object):
    """Trace segmentation wrapper class. In general, this type of model takes a
    calcium trace of length N frames and return a binary segmentation
    of length N frames. e.g. f([0.1, 0.2, ...]) -> [0, 1, ...].

    The expected structure for HDF5 dataset files for this model is as follows:
    Attributes:
        'name': a name for identifying the HDF5 dataset e.g. 'experiment-001'
    Datasets:
        'traces': a matrix of real-valued calcium-traces with shape (no. traces, trace length).
        'spikes': a matrix of corresponding binary spike segmentations with shape (no. traces, trace length).

    # Arguments
        cpdir: checkpoint directory for training artifacts and predictions.
        dataset_attrs_func: function f(hdf5 path) -> dataset attributes.
        dataset_traces_func: function f(hdf5 path) -> dataset calcium traces array
            with shape (no. ROIs x no. frames).
        dataset_spikes_func: function f(hdf5 path) -> dataset binary spikes array
            with shape (no. ROIs x no. frames).
        net_builder_func: function that builds and returns the Keras model for
            training and predictions. This allows swapping out the network
            architecture without re-writing or copying all training and prediction
            code.
    """

    def __init__(self, cpdir='%s/spikes_unet1d' % CHECKPOINTS_DIR,
                 dataset_attrs_func=get_dataset_attrs,
                 dataset_traces_func=get_dataset_traces,
                 dataset_spikes_func=get_dataset_spikes,
                 net_builder_func=unet1d):

        self.cpdir = cpdir
        self.dataset_attrs_func = dataset_attrs_func
        self.dataset_traces_func = dataset_traces_func
        self.dataset_spikes_func = dataset_spikes_func
        self.net_builder_func = net_builder_func

        if not path.exists(self.cpdir):
            mkdir(self.cpdir)

    def fit(self, dataset_paths, shape=(4096,), error_margin=4.,
            batch=20, nb_epochs=20, val_type='random_split', prop_trn=0.8,
            prop_val=0.2, nb_folds=5, keras_callbacks=[], optimizer=Adam(0.002)):
        """Constructs model based on parameters and trains with the given data.
        Internally, the function uses a local function to abstract the training
        for both validation types.

        # Arguments
            dataset_paths: list of paths to HDF5 datasets used for training.
            shape: tuple defining the input length.
            error_margin: number of frames within which a false positive error
                is allowed. e.g. error_margin=1 would allow off-by-1 errors.
            batch: batch size.
            val_type: either 'random_split' or 'cross_validate'.
            prop_trn: proportion of data for training when using random_split.
            prop_val: proportion of data for validation when using random_split.
            nb_folds: number of folds for K-fold cross valdiation using using
                cross_validate.
            keras_callbacks: additional callbacks that should be included.
            nb_epochs: how many epochs. 1 epoch includes 1 sample of every trace.
            optimizer: instantiated keras optimizer.

        # Returns
            history: the keras training history as a dictionary of metrics and
                their values after each epoch.
            model_path: path to the HDF5 file where the best architecture and
                weights were serialized.

        """

        def _fit_single(idxs_trn, idxs_val, model_summary=False):
            """Instantiates model, splits data based on given indices, trains.
            Abstracted in order to enable both random split and cross-validation.
            TODO: abstract this in such a way that allows parallelizing.

            # Returns
                metrics_trn: dictionary of {name: metric} for training data.
                metrics_val: dictionary of {name: metric} for validation data.
                best_model_path: filesystem path to the best serialized model.
            """

            metrics = [F2, prec, reca, ytspks, ypspks]

            def loss(yt, yp):
                return weighted_binary_crossentropy(yt, yp, weightpos=2.0)
            custom_objects = {o.__name__: o for o in metrics + [loss]}

            # Define, compile network.
            model = self.net_builder_func(shape, margin=error_margin)
            model.compile(optimizer=optimizer, loss=loss, metrics=metrics)
            if model_summary:
                model.summary()

            # Split traces and spikes.
            tr_trn = [traces[i] for i in idxs_trn]
            sp_trn = [spikes[i] for i in idxs_trn]
            tr_val = [traces[i] for i in idxs_val]
            sp_val = [spikes[i] for i in idxs_val]

            # 1 epoch = 1 training sample from every trace.
            steps_trn = int(ceil(len(tr_trn) / batch))

            # Training and validation generators.
            gen_trn = self._batch_gen(
                tr_trn, sp_trn, shape, batch, steps_trn, error_margin)
            gen_val = self._batch_gen(
                tr_val, sp_val, shape, len(tr_val) * 2, 1, error_margin)
            x_val, y_val = next(gen_val)

            # Callbacks.
            cpt, spc = (self.cpdir, int(time())), _SamplePlotCallback
            cb = [
                spc('%s/%d_samples_{epoch:03d}_trn.png' % cpt, *next(gen_trn),
                    title='Epoch {epoch: d} val_F2={val_F2: 3f}'),
                spc('%s/%d_samples_{epoch:03d}_val.png' % cpt, x_val, y_val,
                    title='Epoch {epoch:d} val_F2={val_F2:3f}'),
                ModelCheckpoint('%s/%d_model_val_F2_{val_F2:3f}_{epoch:03d}.hdf5' % cpt,
                                monitor='val_F2', mode='max', verbose=1, save_best_only=True),
                CSVLogger('%s/%d_metrics.csv' % cpt),
                MetricsPlotCallback('%s/%d_metrics.png' % cpt)
            ]

            # Train.
            model.fit_generator(gen_trn, steps_per_epoch=steps_trn,
                                epochs=nb_epochs, callbacks=cb,
                                validation_data=(x_val, y_val), verbose=1)

            # Identify best serialized model, assuming newest is best.
            model_path_glob = '%s/%d_model*hdf5' % cpt
            model_paths = sorted(glob(model_path_glob), key=os.path.getmtime)
            best_model_path = model_paths[-1]

            # Training and validation metrics on trained model.
            model.load_weights(best_model_path)
            mt = model.evaluate_generator(gen_trn, steps_trn)
            mt = {n: m for n, m in zip(model.metrics_names, mt)}
            mv = model.evaluate(x_val, y_val)
            mv = {n: m for n, m in zip(model.metrics_names, mv)}

            return mt, mv, best_model_path
            # END OF INTERNAL FUNCTION.

        logger = logging.getLogger(funcname())

        # Error check.
        assert len(shape) == 1
        assert val_type in ['random_split', 'cross_validate']
        assert nb_folds > 1
        assert prop_trn + prop_val == 1.

        # Extract traces and spikes from datasets.
        traces = [t for p in dataset_paths for t in self.dataset_traces_func(p)]
        spikes = [s for p in dataset_paths for s in self.dataset_spikes_func(p)]
        assert len(traces) == len(spikes)

        # Random-split training.
        if val_type == 'random_split':

            idxs = rng.choice(np.arange(len(traces)), len(traces), replace=0)
            idxs_trn = idxs[:int(len(idxs) * prop_trn)]
            idxs_val = idxs[-1 * int(len(idxs) * prop_val):]
            mt, mv, bmp = _fit_single(idxs_trn, idxs_val, True)
            for k in sorted(mt.keys()):
                s = (k, mt[k], mv[k])
                logger.info('%-20s trn=%-9.4lf val=%-9.4lf' % s)
            logger.info('Best model path: %s' % bmp)

        # Cross-validation training.
        elif val_type == 'cross_validate':

            # Randomly-ordered indicies for cross-validation.
            idxs = rng.choice(np.arange(len(traces)), len(traces), replace=0)
            fsz = int(len(idxs) / nb_folds)
            fold_idxs = [idxs[fsz * n:fsz * n + fsz] for n in range(nb_folds)]

            # Train on folds.
            metrics_trn, metrics_val = [], []
            for val_idx in range(nb_folds):

                # Seperate training and validation indexes.
                idxs_trn = [idx for i, fold in enumerate(fold_idxs)
                            if i != val_idx for idx in fold]
                idxs_val = [idx for i, fold in enumerate(fold_idxs)
                            if i == val_idx for idx in fold]
                assert set(idxs_trn).intersection(idxs_val) == set([])

                # Train and report metrics.
                logger.info('\nCross validation fold = %d' % val_idx)
                mt, mv, _ = _fit_single(idxs_trn, idxs_val, val_idx == 0)
                metrics_trn.append(mt)
                metrics_val.append(mv)

                for k in sorted(mt.keys()):
                    s = (k, mt[k], mv[k])
                    logger.info('%-20s trn=%-10.4lf val=%-10.4lf' % s)

            # Aggregate metrics.
            logger.info('\nCross validation summary')
            for k in sorted(metrics_trn[0].keys()):
                vals_trn = [m[k] for m in metrics_trn]
                vals_val = [m[k] for m in metrics_val]
                s = (k, np.mean(vals_trn), np.std(vals_trn),
                     np.mean(vals_val), np.std(vals_val))
                logger.info('%-20s trn=%-9.4lf (%.4lf) val=%-9.4lf (%.4lf)' % s)

    def _batch_gen(self, traces, spikes, shape, batch_size, nb_steps, margin):

        # Apply the error margin by max pooling the spikes once up-front.
        lens = [len(x) for x in spikes]
        for l in np.unique(lens):
            idxs, = np.where(lens == l)
            x = np.vstack([spikes[i] for i in idxs])
            x = K.variable(x.astype(np.float32))
            x = K.expand_dims(K.expand_dims(x, axis=0), axis=-1)
            x = K.pool2d(x, (1, margin + 1), padding='same')
            x = K.get_value(x[0, :, :, 0])
            for i in range(x.shape[0]):
                spikes[idxs[i]] = x[i]

        while True:

            idxs = np.arange(len(traces))
            cidxs = cycle(rng.choice(idxs, len(idxs), replace=False))

            for _ in range(nb_steps):

                # Empty batches (traces and spikes).
                tb = np.zeros((batch_size,) + shape, dtype=np.float64)
                sb = np.zeros((batch_size,) + shape, dtype=np.uint8)

                for bidx in range(batch_size):

                    # Dataset and sample indices.
                    idx = next(cidxs)

                    # Pick start and end point around positive spike index.
                    x0 = rng.randint(0, len(spikes[idx]) - shape[0])
                    x1 = x0 + shape[0]

                    # Populate batch.
                    tb[bidx] = traces[idx][x0:x1]
                    sb[bidx] = spikes[idx][x0:x1]

                yield tb, sb

    def predict(self, dataset_paths, model_path, batch=32, threshold=0.5):
        """Prediction on new datasets.

        Note: if the model used an error margin > 0, you should do one of the
        following when comparing to ground-truth segmentation masks.
        1. Apply the same error margin to the ground-truth masks.
        2. Take the local max of each "stripe" of pooled predictions as the
        actual prediction. skimage measure.label and morphology.local_maxima
        might be helpful for this.

        # Arguments
            dataset_paths: list of paths to the HDF5 datasets that will be predicted.
            model_path: path to the serialized model used for predicting.
            batch: batch size for predictions. Adjust based on GPU.
            threshold: prediction threshold for rounding. 0.5 is equivalent to
                just calling .round() on the network outputs.

        # Returns
            spikes_pred_all: list of spike prediction matrices, one per dataset.
                Shape is (no. ROIs x length of traces).
            names_all: the corresponding name for each dataset from its HDF5 file.

        """
        spikes_pred_all = []
        names_all = []

        for p in dataset_paths:
            attrs = self.dataset_attrs_func(p)
            names_all.append(attrs['name'])
            traces = self.dataset_traces_func(p)
            input_shape = (traces.shape[1],)
            model = load_model_with_new_input_shape(
                model_path, input_shape=input_shape, compile=False)
            spikes_pred = model.predict(traces, batch_size=batch)
            spikes_pred = (spikes_pred > threshold).astype(np.uint8)
            spikes_pred_all.append(spikes_pred)

        return spikes_pred_all, names_all
