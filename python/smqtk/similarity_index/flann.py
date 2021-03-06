__author__ = 'purg'

import cPickle
import logging
import multiprocessing
import numpy
import os
import os.path as osp
import tempfile

from smqtk.similarity_index import (
    SimilarityIndex,
    SimilarityIndexStateLoadError,
    SimilarityIndexStateSaveError,
)
from smqtk.utils import safe_create_dir

from smqtk_config import DATA_DIR, WORK_DIR

try:
    import pyflann
except ImportError:
    pyflann = None


class FlannSimilarity (SimilarityIndex):
    """
    Nearest-neighbor computation using the FLANN library (pyflann module).

    This implementation uses in-memory data structures, and thus has an index
    size limit based on how much memory the running machine has available.

    NOTE: Normally, FLANN indices don't play well with multiprocessing due to
        being C structures and don't transfer into new processes memory space.
        However, FLANN can serialize an index, and so this processes uses
        temporary storage space to serialize

    """

    @classmethod
    def is_usable(cls):
        # Assuming that if the pyflann module is available, then it's going to
        # work. This assumption will probably be invalidated in the future...
        return pyflann is not None

    def __init__(self, save_dir, temp_dir=tempfile.gettempdir(), autotune=False,
                 target_precision=0.95, sample_fraction=0.1,
                 distance_method='hik', random_seed=None):
        """
        Initialize FLANN index properties. Index is of course not build yet (no
        data).

        Optional parameters are for when building the index. Documentation on
        their meaning can be found in the FLANN documentation PDF:

            http://www.cs.ubc.ca/research/flann/uploads/FLANN/flann_manual-1.8.4.pdf

        See the MATLAB section for detailed descriptions (python section will
        just point you to the MATLAB section).

        :param save_dir: Directory to use for save/load index file operations.
            If relative, interpreted against DATA_DIR as configured in
            `smqtk_config` module.
        :type save_dir: str
        :param temp_dir: Directory to use for working file storage, mainly for
            saving and loading indices for multiprocess transitions. By default,
            this is the platform specific standard temporary file directory. If
            given a relative path, it is interpreted against WORK_DIR as
            configured in `smqtk_config` module.
        :type temp_dir: str
        :param autotune: Whether or not to perform parameter auto-tuning when
            building the index. If this is False, then the `target_precision`
            and `sample_fraction` parameters are not used.
        :type autotune: bool
        :param target_precision: Target estimation accuracy when determining
            nearest neighbor when tuning parameters. This should be between
            [0,1] and represents percentage accuracy.
        :type target_precision: float
        :param sample_fraction: Sub-sample percentage of the total index to use
            when performing auto-tuning. Value should be in the range of [0,1]
            and represents percentage.
        :type sample_fraction: float
        :param distance_method: Method label of the distance function to use.
            See FLANN documentation manual for available methods. Common methods
            include "hik", "chi_square" (default), and "euclidean".
        :type distance_method: str

        """
        self._save_dir = osp.abspath(osp.join(DATA_DIR,
                                              osp.expanduser(save_dir)))
        self._temp_dir = osp.abspath(osp.join(WORK_DIR,
                                              osp.expanduser(temp_dir)))

        # Standard save files relative to save directory
        self._sf_flann_index = osp.join(self._save_dir, "flann.index")
        self._sf_state = osp.join(self._save_dir, "flann.state.pickle")

        self._build_autotune = bool(autotune)
        self._build_target_precision = float(target_precision)
        self._build_sample_frac = float(sample_fraction)

        self._distance_method = str(distance_method)

        # The flann instance with a built index. None before index construction
        #: :type: pyflann.index.FLANN or None
        self._flann = None
        # Flann index parameters determined during building. This is used when
        # re-building index when adding to it.
        #: :type: dict
        self._flann_build_params = None
        # Path to the file that is the serialization of our index. This is None
        # before index construction
        #: :type: None or str
        self._flann_index_cache = None
        # The process ID that the currently set FLANN instance was build/loaded
        # on. If this differs from the current process ID, the index should be
        # reloaded from cache.
        self._pid = None

        # In-order cache of descriptors we're indexing over.
        # - flann.nn_index will spit out indices into this list
        # - This should be preserved when forking processes, i.e. shouldn't have
        #   to save to file like we do with FLANN index.
        #: :type: list[smqtk.data_rep.DescriptorElement]
        self._descr_cache = None

        self._rand_seed = None if random_seed is None else int(random_seed)

    def _restore_index(self):
        """
        If we think we're suppose to have an index, check the recorded PID with
        the current PID, reloading the index from cache if they differ.

        If there is a loaded index and we're on the same process that created it
        this does nothing.
        """
        if self._flann_index_cache and os.path.isfile(self._flann_index_cache) \
                and self._pid != multiprocessing.current_process().pid:
            pyflann.set_distance_type(self._distance_method)
            self._flann = pyflann.FLANN()

            pts_array = [d.vector() for d in self._descr_cache]
            pts_array = numpy.array(pts_array, dtype=pts_array[0].dtype)
            self._flann.load_index(self._flann_index_cache, pts_array)
            self._pid = multiprocessing.current_process().pid

    def count(self):
        """
        :return: Number of elements in this index.
        :rtype: int
        """
        return len(self._descr_cache) if self._descr_cache else 0

    def build_index(self, descriptors):
        """
        Build the index over the descriptors data elements.

        Subsequent calls to this method should rebuild the index, not add to it.

        Implementation Notes:
            - We keep a cache file serialization around for our index in case
                sub-processing occurs so as to be able to recover from the
                underlying C data not being there. This could cause issues if
                a main or child process rebuild's the index, as we clear the old
                cache away.

        :raises ValueError: No data available in the given iterable.

        :param descriptors: Iterable of descriptors elements to build index over.
        :type descriptors: collections.Iterable[smqtk.data_rep.DescriptorElement]

        """
        # If there is already an index, clear the cache file if we are in the
        # same process that created our current index.
        if self._flann_index_cache and os.path.isfile(self._flann_index_cache) \
                and self._pid == multiprocessing.current_process().pid:
            self._log.debug('removing old index cache file')
            os.remove(self._flann_index_cache)

        self._log.debug("Building new index")

        # Compute descriptors for data elements
        self._log.debug("Computing descriptors for data")
        # uid2vec = \
        #     self._content_descriptor.compute_descriptor_async(data)
        # Translate returned mapping into cache lists
        self._descr_cache = [d for d in sorted(descriptors,
                                               key=lambda e: e.uuid())]
        if not self._descr_cache:
            raise ValueError("No data provided in given iterable.")

        # numpy array version for FLANN
        pts_array = [d.vector() for d in self._descr_cache]
        pts_array = numpy.array(pts_array, dtype=pts_array[0].dtype)

        # Reset PID/FLANN/saved cache
        self._pid = multiprocessing.current_process().pid
        safe_create_dir(self._temp_dir)
        fd, self._flann_index_cache = tempfile.mkstemp(".flann",
                                                       dir=self._temp_dir)
        os.close(fd)
        self._log.debug("Building FLANN index")
        params = {
            "algorithm": self._build_autotune,
            "target_precision": self._build_target_precision,
            "sample_fraction": self._build_sample_frac,
            "log_level": ("info"
                          if self._log.getEffectiveLevel() <= logging.DEBUG
                          else "warn")
        }
        if self._rand_seed is not None:
            params['random_seed'] = self._rand_seed
        pyflann.set_distance_type(self._distance_method)
        self._flann = pyflann.FLANN()
        self._flann_build_params = self._flann.build_index(pts_array, **params)

        # Saving out index cache
        self._log.debug("Saving index to cache file: %s",
                        self._flann_index_cache)
        self._flann.save_index(self._flann_index_cache)

    def save_index(self):
        """
        Save the current index state to a configured location. This
        configuration should be set at instance construction.

        This will overwrite previously saved state date given the same
        configuration.

        :raises SimilarityIndexStateSaveError: Unable to save the current index
            state for some reason.

        """
        self._restore_index()
        if self._flann is None:
            raise SimilarityIndexStateSaveError("No index built yet to save")

        safe_create_dir(self._save_dir)
        self._flann.save_index(self._sf_flann_index)

        state = {
            "flann_params": self._flann_build_params,
            "descr_cache": self._descr_cache,
            "distance_method": self._distance_method,
            "rand_seed": self._rand_seed
        }
        with open(self._sf_state, 'wb') as f:
            cPickle.dump(state, f)

    def load_index(self):
        """
        Load a saved index state based on the current configuration.

        :raises SimilarityIndexStateLoadError: Could not load index state.

        """
        self._restore_index()

        if False in (osp.isfile(self._sf_flann_index),
                     osp.isfile(self._sf_state)):
            raise SimilarityIndexStateLoadError("In complete index save state")

        with open(self._sf_state, 'rb') as f:
            state = cPickle.load(f)

        self._distance_method = state['distance_method']
        self._rand_seed = state['rand_seed']
        self._descr_cache = state['descr_cache']
        self._flann_build_params = state['flann_params']

        pts_array = [d.vector() for d in self._descr_cache]
        pts_array = numpy.array(pts_array, dtype=pts_array[0].dtype)

        pyflann.set_distance_type(self._distance_method)
        self._flann = pyflann.FLANN()
        self._flann.load_index(self._sf_flann_index, pts_array)

    def nn(self, d, n=1):
        """
        Return the nearest `N` neighbors to the given descriptor element.

        :param d: Descriptor element to compute the neighbors of.
        :type d: smqtk.data_rep.DescriptorElement

        :param n: Number of nearest neighbors to find.
        :type n: int

        :return: Tuple of nearest N DescriptorElement instances, and a tuple of
            the distance values to those neighbors.
        :rtype: (tuple[smqtk.data_rep.DescriptorElement], tuple[float])

        """
        self._restore_index()
        vec = d.vector()

        # If the distance method is HIK, we need to treat it special since that
        # method produces a similarity score, not a distance score.
        #   -
        #
        # FLANN asserts that we query for <= index size, thus the use of min()
        if self._distance_method == 'hik':
            #: :type: numpy.core.multiarray.ndarray, numpy.core.multiarray.ndarray
            idxs, dists = self._flann.nn_index(vec, len(self._descr_cache),
                                               **self._flann_build_params)

        else:
            #: :type: numpy.core.multiarray.ndarray, numpy.core.multiarray.ndarray
            idxs, dists = self._flann.nn_index(vec,
                                               min(n, len(self._descr_cache)),
                                               **self._flann_build_params)

        # When N>1, return value is a 2D array. Since this method limits query
        #   to a single descriptor, we reduce to 1D arrays.
        if len(idxs.shape) > 1:
            idxs = idxs[0]
            dists = dists[0]
        if self._distance_method == 'hik':
            idxs = tuple(reversed(idxs))[:n]
            dists = tuple(reversed(dists))[:n]
        return [self._descr_cache[i] for i in idxs], dists


SIMILARITY_NN_CLASS = FlannSimilarity
