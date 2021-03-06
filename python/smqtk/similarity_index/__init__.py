"""
Interface for generic element-wise nearest-neighbor computation.
"""
__author__ = 'purg'

import abc
import logging


class SimilarityIndexStateSaveError (Exception):
    """
    Exception for when as index was unable to be saved.
    """
    pass


class SimilarityIndexStateLoadError (Exception):
    """
    Exception for when an index state cannot be loaded for whatever reason
    """
    pass


class SimilarityIndex (object):
    """
    Common interface for descriptor-based nearest-neighbor computation.
    """
    __metaclass__ = abc.ABCMeta

    @classmethod
    @abc.abstractmethod
    def is_usable(cls):
        """
        Check whether this implementation is available for use.

        Since certain implementations may require additional dependencies that
        may not yet be available on the system, this method should check for
        those dependencies and return a boolean saying if the implementation is
        usable.

        NOTES:
            - This should be a class method
            - When not available, this should emit a warning message pointing to
                documentation on how to get/install required dependencies.

        :return: Boolean determination of whether this implementation is usable.
        :rtype: bool

        """
        return

    @property
    def _log(self):
        return logging.getLogger('.'.join([self.__module__,
                                           self.__class__.__name__]))

    @abc.abstractmethod
    def count(self):
        """
        :return: Number of elements in this index.
        :rtype: int
        """
        return

    @abc.abstractmethod
    def build_index(self, descriptors):
        """
        Build the index over the descriptor data elements.

        Subsequent calls to this method should rebuild the index, not add to it.

        :raises ValueError: No data available in the given iterable.

        :param descriptors: Iterable of descriptor elements to build index over.
        :type descriptors: collections.Iterable[smqtk.data_rep.DescriptorElement]

        """
        return

    @abc.abstractmethod
    def save_index(self):
        """
        Save the current index state to a configured location. This
        configuration should be set at instance construction.

        This will overwrite a previously saved state given the same
        configuration.

        :raises SimilarityIndexStateSaveError: Unable to save the current index
            state for some reason.

        """
        return

    @abc.abstractmethod
    def load_index(self):
        """
        Load a saved index state based on the current configuration.

        :raises SimilarityIndexStateLoadError: Could not load index state.

        """
        return

    @abc.abstractmethod
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
        return


def get_similarity_nn():
    """
    Discover and return SimilarityNN implementation classes found in the given
    plugin search directory. Keys in the returned map are the names of the
    discovered classes, and the paired values are the actual class type objects.

    We look for modules (directories or files) that start with an alphanumeric
    character ('_' prefixed files/directories are hidden, but not recommended).

    Within a module we first look for a helper variable by the name
    ``SIMILARITY_NN_CLASS``, which can either be a single class object or
    an iterable of class objects, to be exported. If the variable is set to
    None, we skip that module and do not import anything. If the variable is not
    present, we look for a class by the same name and casing as the module. If
    neither are found, the module is skipped.

    :return: Map of discovered class object of type ``SimilarityNN`` whose
        keys are the string names of the classes.
    :rtype: dict of (str, type)

    """
    from smqtk.utils.plugin import get_plugins
    import os
    this_dir = os.path.abspath(os.path.dirname(__file__))
    helper_var = "SIMILARITY_NN_CLASS"

    def class_filter(cls):
        log = logging.getLogger('.'.join([__name__, 'get_similarity_nn',
                                          'class_filter']))
        if not cls.is_usable():
            log.warn("Class type '%s' not usable, filtering out...",
                     cls.__name__)
            return False
        return True

    return get_plugins(__name__, this_dir, helper_var, SimilarityIndex,
                       class_filter)
