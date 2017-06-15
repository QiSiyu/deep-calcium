# Convert neurofinder datasets to MP4 videos.
# Save the videos in their respective dataset directories.
# Takes a long time to run for all the datasets.
from os.path import exists
import logging
import sys
sys.path.append('.')

from deepcalcium.datasets.nf import nf_load_hdf5, neurofinder_names
from deepcalcium.utils.visuals import dataset_to_mp4

logging.basicConfig(level=logging.INFO)

names = ['neurofinder.04.00', 'neurofinder.04.00.test', 'neurofinder.04.01']
datasets = nf_load_hdf5(names)
for ds in datasets:
    s = ds.get('series/raw')[...]
    m = ds.get('masks/raw')[...] if 'masks/raw' in ds else None
    mp4_path = ds.filename.replace('.hdf5', '.mp4')
    if not exists(mp4_path):
        dataset_to_mp4(s, m, mp4_path)
    ds.close()
