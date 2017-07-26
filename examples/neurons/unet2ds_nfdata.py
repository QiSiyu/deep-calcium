# Neurofinder training and prediction using UNet 2D Summary model.
from time import time
import argparse
import logging
import numpy as np
import tensorflow as tf

import sys
sys.path.append('.')

from deepcalcium.models.neurons.unet_2d_summary import UNet2DSummary
from deepcalcium.datasets.nf import nf_load_hdf5, nf_submit
from deepcalcium.utils.runtime import funcname

np.random.seed(865)
tf.set_random_seed(7535)
logging.basicConfig(level=logging.INFO)


def training(dataset_name, model_path, cpdir):
    """Train on neurofinder datasets."""

    # Load all sequences and masks as hdf5 File objects.
    dspaths = nf_load_hdf5(dataset_name)

    # Setup model.
    model = UNet2DSummary(cpdir=cpdir)

    # Training.
    return model.fit(
        dspaths,                    # paths to hdf5 datasets.
        model_path=model_path,      # Keras architecture and weights.
        shape_trn=(128, 128),       # Input/output windows to the network.
        shape_val=(512, 512),
        batch_size_trn=20,          # Batch size.
        nb_steps_trn=100,           # Training batches / epoch.
        nb_epochs=10,               # Epochs.
        keras_callbacks=[],         # Custom keras callbacks.
        prop_trn=0.75,              # Height % for training, validation.
        prop_val=0.25,
    )


def evaluation(dataset_name, model_path, cpdir):
    """Evaluate datasets - once without test-time augmentation and once with."""

    logger = logging.getLogger(funcname())
    ds_trn = nf_load_hdf5(dataset_name)
    model = UNet2DSummary(cpdir=cpdir)

    for aug in [True, False]:
        logger.info('Evaluation with%s.' % (' TTA' if aug else 'out TTA'))
        # Evaluate training data performance using neurofinder metrics.
        model.predict(
            ds_trn,
            model_path=model_path,
            window_shape=(512, 512),
            save=True,
            print_scores=True,
            augmentation=aug,          # Test-time augmentation.
        )


def prediction(dataset_name, model_path, cpdir):
    """Predictions on given datasets with and without test-time augmentation."""

    logger = logging.getLogger(funcname())
    ds_tst = nf_load_hdf5(dataset_name)
    model = UNet2DSummary(cpdir=cpdir)
    tic = int(time())

    for aug in [True, False]:
        logger.info('Prediction with%s.' % (' TTA' if aug else 'out TTA'))

        # Returns predictions as list of numpy arrays.
        Mp, names = model.predict(
            ds_tst,                      # hdf5 sequences (no masks).
            model_path=model_path,       # Pre-trained model and weights.
            window_shape=(512, 512),     # Input/output windows to the network.
            save=False,
            augmentation=aug
        )

        # Round the activations.
        Mp = [m.round() for m in Mp]

        # Make a submission from the predicted masks.
        json_path = '%s/submission_%d%s.json' % (
            model.cpdir, tic, ('_TTA' if aug else ''))
        nf_submit(Mp, names, json_path)
        json_path = '%s/submission_latest%s.json' % (
            model.cpdir, ('_TTA' if aug else ''))
        nf_submit(Mp, names, json_path)

if __name__ == "__main__":

    ap = argparse.ArgumentParser(description='CLI for UNet 2D Summary example.')
    sp = ap.add_subparsers(title='actions', description='Choose an action.')

    cpdir = 'checkpoints/unet2ds_nf'

    # Training cli.
    sp_trn = sp.add_parser('train', help='CLI for training.')
    sp_trn.set_defaults(which='train')
    sp_trn.add_argument('dataset', help='dataset name', default='all_train')
    sp_trn.add_argument('-m', '--model', help='path to model')
    sp_trn.add_argument(
        '-c', '--cpdir', help='checkpoint directory', default=cpdir)

    # Training cli.
    sp_eva = sp.add_parser('evaluate', help='CLI for training.')
    sp_eva.set_defaults(which='evaluate')
    sp_eva.add_argument('dataset', help='dataset name', default='all_train')
    sp_eva.add_argument('-m', '--model', help='path to model', required=True)
    sp_eva.add_argument(
        '-c', '--cpdir', help='checkpoint directory', default=cpdir)

    # Prediction cli.
    sp_prd = sp.add_parser('predict', help='CLI for prediction.')
    sp_prd.set_defaults(which='predict')
    sp_prd.add_argument('dataset', help='dataset name', default='all')
    sp_prd.add_argument('-m', '--model', help='path to model', required=True)
    sp_prd.add_argument(
        '-c', '--cpdir', help='checkpoint directory', default=cpdir)

    # Parse and run appropriate function.
    args = vars(ap.parse_args())

    if args['which'] == 'train':
        training(args['dataset'], args['model'], args['cpdir'])

    if args['which'] == 'evaluate':
        evaluation(args['dataset'], args['model'], args['cpdir'])

    if args['which'] == 'predict':
        prediction(args['dataset'], args['model'], args['cpdir'])
