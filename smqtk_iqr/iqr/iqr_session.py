import io
import json
import logging
import threading
from types import TracebackType
from typing import (
    cast, Dict, Hashable, Iterable, List, Optional, Set, Tuple, Union, Sequence
)
import uuid
import zipfile

import numpy as np

from smqtk_indexing import NearestNeighborsIndex
from smqtk_relevancy import RankRelevancyWithFeedback
from smqtk_descriptors.impls.descriptor_set.memory import MemoryDescriptorSet
from smqtk_descriptors import (
    DescriptorElement, DescriptorElementFactory
)


class IqrSession ():
    """
    Encapsulation of IQR Session related data structures with a centralized
    lock for multi-thread access.

    This object is compatible with the python with-statement, so when elements
    are to be used or modified, it should be within a with-block so race
    conditions do not occur across threads/sub-processes.

    """

    @property
    def _log(self) -> logging.Logger:
        return logging.getLogger(
            '.'.join((self.__module__, self.__class__.__name__)) +
            "[%s]" % self.uuid
        )

    def __init__(
        self, rank_relevancy_with_feedback: RankRelevancyWithFeedback,
        pos_seed_neighbors: int = 500,
        session_uid: Optional[Union[str, uuid.UUID]] = None
    ) -> None:
        """
        Initialize the IQR session

        This does not initialize the working set for ranking as there are no
        known positive descriptor examples at this time.

        Adjudications
        -------------
        Adjudications are carried through between initializations. This allows
        indexed material adjudicated through-out the lifetime of the session to
        stay relevant.

        :param rank_relevancy_with_feedback: The rank relevancy with feedback
            algorithm used to rank user adjudications.

        :param pos_seed_neighbors: Number of neighbors to pull from the given
            ``nn_index`` for each positive exemplar when populating the working
            set, i.e. this value determines the size of the working set for
            IQR refinement. By default, we try to get 500 neighbors.

            Since there may be partial to significant overlap of near neighbors
            as a result of nn_index queries for positive exemplars, the working
            set may contain anywhere from this value's number of entries, to
            ``N*P``, where ``N`` is this value and ``P`` is the number of
            positive examples at the time of working set initialization.

        :param session_uid: Optional manual specification of session UUID. By
            default this will be a string UUID as generated by
            ``uuid.uuid1()``.

        """
        self.uuid = session_uid or str(uuid.uuid1()).replace('-', '')
        self.lock = threading.RLock()

        self.pos_seed_neighbors = int(pos_seed_neighbors)

        # Local descriptor set for ranking, populated by a query to the
        #   nn_index instance.
        # Added external data/descriptors not added to this set.
        self.working_set = MemoryDescriptorSet()

        # Book-keeping set so we know what positive descriptors
        # UUIDs we've used to query the neighbor index with already.
        self._wi_seeds_used: Set[Hashable] = set()

        # Descriptor elements representing data from external sources.
        # These may be arbitrary descriptor elements not present in
        #   ``working_index``.
        self.external_positive_descriptors: Set[DescriptorElement] = set()
        self.external_negative_descriptors: Set[DescriptorElement] = set()

        # Descriptor references from ``working_set`` that have been
        #   adjudicated.
        # These should be sub-sets of the descriptors contained in the
        #   ``working_set``.
        self.positive_descriptors: Set[DescriptorElement] = set()
        self.negative_descriptors: Set[DescriptorElement] = set()

        # Sets of descriptor elements that were used in the last refinement
        #   to achieve the currently cached results, i.e. "contributed" to the
        #   current results state.
        # These sets are empty before the first refine after construction or a
        #   reset.
        self.rank_contrib_pos: Set[DescriptorElement] = set()
        self.rank_contrib_pos_ext: Set[DescriptorElement] = set()
        self.rank_contrib_neg: Set[DescriptorElement] = set()
        self.rank_contrib_neg_ext: Set[DescriptorElement] = set()

        # Mapping of a DescriptorElement in our relevancy search index (not the
        #   set that the nn_index uses) to the relevancy score given the
        #   recorded positive and negative adjudications.
        # This is None before any initialization or refinement occurs.
        self.results: Optional[Dict[DescriptorElement, float]] = None

        # List of UID's representing the descriptors that we recommend for
        #   adjudicationfeedback.
        # This is None before any initialization or refinement occurs.
        self.feedback_list: Optional[Sequence[DescriptorElement]] = None

        # Cache variables for views of refinement results.
        # All results as a list in order of relevancy score.
        self._ordered_results: Optional[
            Sequence[Tuple[DescriptorElement, float]]
        ] = None
        #: Positively adjudicated descriptors in order of relevancy score.
        self._ordered_pos: Optional[
            Sequence[Tuple[DescriptorElement, float]]
        ] = None
        # Negatively adjudicated descriptors in order of relevancy score.
        self._ordered_neg: Optional[
            Sequence[Tuple[DescriptorElement, float]]
        ] = None
        # Non-adjudicated descriptors in our working set in order of
        # relevancy score.
        self._ordered_non_adj: Optional[
            Sequence[Tuple[DescriptorElement, float]]
        ] = None

        #
        # Algorithm Instances [+Config]
        #
        # RankRelvancy instance that is used for producing results.
        self.rank_relevancy_with_feedback = rank_relevancy_with_feedback

    def __enter__(self) -> "IqrSession":
        self.lock.acquire()
        return self

    # noinspection PyUnusedLocal
    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType]
    ) -> None:
        self.lock.release()

    def external_descriptors(
        self,
        positive: Iterable[DescriptorElement] = (),
        negative: Iterable[DescriptorElement] = ()
    ) -> None:
        """
        Add positive/negative descriptors from external data.

        These descriptors may not be a part of our working set.

        TODO: Add ability to "remove" positive/negative external descriptors.
              See ``adjudicate`` method "un_..." parameters.

        :param positive: Iterable of descriptors from external sources to
            consider positive examples.
        :param negative: Iterable of descriptors from external sources to
            consider negative examples.
            collections.abc.Iterable[smqtk_descriptors.DescriptorElement]

        """
        positive = set(positive)
        negative = set(negative)
        with self.lock:
            self.external_positive_descriptors.update(positive)
            self.external_positive_descriptors.difference_update(negative)

            self.external_negative_descriptors.update(negative)
            self.external_negative_descriptors.difference_update(positive)

    def adjudicate(
        self,
        new_positives: Iterable[DescriptorElement] = (),
        new_negatives: Iterable[DescriptorElement] = (),
        un_positives: Iterable[DescriptorElement] = (),
        un_negatives: Iterable[DescriptorElement] = ()
    ) -> None:
        """
        Update current state of working set positive and negative
        adjudications based on descriptor UUIDs.

        If the same descriptor element is listed in both new positives and
        negatives, they cancel each other out, causing that descriptor to not
        be included in the adjudication.

        The given iterables must be re-traversable. Otherwise the given
        descriptors will not be properly registered.

        :param new_positives: Descriptors of elements in our working set to
            now be considered to be positively relevant.
            collections.abc.Iterable[smqtk_descriptors.DescriptorElement]

        :param new_negatives: Descriptors of elements in our working set to
            now be considered to be negatively relevant.
            collections.abc.Iterable[smqtk_descriptors.DescriptorElement]

        :param un_positives: Descriptors of elements in our working set to now
            be considered not positive any more.
            collections.abc.Iterable[smqtk_descriptors.DescriptorElement]

        :param un_negatives: Descriptors of elements in our working set to now
            be considered not negative any more.
            collections.abc.Iterable[smqtk_descriptors.DescriptorElement]

        """
        # TODO: Assert that inputs are indeed in the working set?

        new_positives = set(new_positives)
        new_negatives = set(new_negatives)
        un_positives = set(un_positives)
        un_negatives = set(un_negatives)

        with self.lock:
            pos_before = set(self.positive_descriptors)
            self.positive_descriptors.update(new_positives)
            self.positive_descriptors.difference_update(un_positives)
            self.positive_descriptors.difference_update(new_negatives)
            pos_changed = pos_before != self.positive_descriptors
            if pos_changed:
                # Reset ordered positives cache if pos adjudications changed.
                self._ordered_pos = None

            neg_before = set(self.negative_descriptors)
            self.negative_descriptors.update(new_negatives)
            self.negative_descriptors.difference_update(un_negatives)
            self.negative_descriptors.difference_update(new_positives)
            neg_changed = neg_before != self.negative_descriptors
            if neg_changed:
                # Reset ordered negatives cache if neg adjudications changed.
                self._ordered_neg = None

            if pos_changed or neg_changed:
                # Reset non-adjudicated cache if anything changed.
                self._ordered_non_adj = None

    def update_working_set(self, nn_index: NearestNeighborsIndex) -> None:
        """
        Initialize or update our current working set using the given
        :class:`.NearestNeighborsIndex` instance given our current positively
        labeled descriptor elements.

        We only query from the index for new positive elements since the last
        update or reset.

        :param nn_index: :class:`.NearestNeighborsIndex` to query from.

        :raises RuntimeError: There are no positive example descriptors in this
            session to use as a basis for querying.

        """
        pos_examples = (self.external_positive_descriptors |
                        self.positive_descriptors)
        if len(pos_examples) == 0:
            raise RuntimeError("No positive descriptors to query the neighbor "
                               "index with.")

        # adding to working set
        self._log.info("Building working set using %d positive examples "
                       "(%d external, %d adjudicated)",
                       len(pos_examples),
                       len(self.external_positive_descriptors),
                       len(self.positive_descriptors))
        # TODO: parallel_map and reduce with merge-dict
        for p in pos_examples:
            if p.uuid() not in self._wi_seeds_used:
                self._log.debug("Querying neighbors to: %s", p)
                self.working_set.add_many_descriptors(
                    nn_index.nn(p, n=self.pos_seed_neighbors)[0]
                )
                self._wi_seeds_used.add(p.uuid())

    def refine(self) -> None:
        """ Refine current model results based on current adjudication state

        :raises RuntimeError: No working set has been initialized.
            :meth:`update_working_set` should have been called after
            adjudicating some positive examples.
        :raises RuntimeError: There are no adjudications to run on. We must
            have at least one positive adjudication.

        """
        with self.lock:
            # combine pos/neg adjudications + added external data descriptors
            pos = [desc.vector() for desc in (self.positive_descriptors |
                                              self.external_positive_descriptors)]

            neg = [desc.vector() for desc in (self.negative_descriptors |
                                              self.external_negative_descriptors)]

            if not pos:
                raise RuntimeError("Did not find at least one positive "
                                   "adjudication.")

            self._log.debug("Ranking working set with %d pos and %d neg total "
                            "examples.", len(pos), len(neg))
            pool_uids, pool_de = zip(*self.working_set.items())
            pool = [de.vector() for de in pool_de]
            probabilities, feedback_uuids = self.rank_relevancy_with_feedback.rank_with_feedback(
                pos, neg, pool, pool_uids)
            self.results = dict(zip(self.working_set.iterdescriptors(), probabilities))
            self.feedback_list = [*self.working_set.get_many_descriptors(feedback_uuids)]
            # Record UIDs of elements used for relevancy ranking.
            # - shallow copy for separate container instance
            self.rank_contrib_pos = set(self.positive_descriptors)
            self.rank_contrib_pos_ext = set(self.external_positive_descriptors)
            self.rank_contrib_neg = set(self.negative_descriptors)
            self.rank_contrib_neg_ext = set(self.external_negative_descriptors)
            # Clear result view caches
            self._ordered_results = self._ordered_pos = self._ordered_neg = \
                self._ordered_non_adj = None

    def ordered_results(self) -> List[Tuple[DescriptorElement, float]]:
        """
        Return a tuple of all working-set descriptor elements as tuples of
        ``(element, score)`` in order of descending relevancy score.

        If refinement has not yet occurred since session creation or the last
        reset, an empty tuple is returned.
        """
        with self.lock:
            try:
                return list(cast(List, self._ordered_results))
            except TypeError:
                # NoneType is not iterable
                # Cache did non exist.
                if self.results is None:
                    # NoneType missing items/iteritems attr
                    # No results to iterate over.
                    return list()

                result_items = self.results.items()
                r = self._ordered_results = cast(
                    List[Tuple[DescriptorElement, float]],
                    sorted(
                        result_items,
                        key=lambda p: p[1], reverse=True
                    )
                )
                # Shallow copy of the list to protect against external mutation
                return list(r)

    def feedback_results(self) -> List[DescriptorElement]:
        """
        Return a list of all working-set descriptor elements that would benefit
        from further refinement. The list is in order of most to least useful.

        If refinement has not yet occurred since session creation or the last
        reset, an empty tuple is returned.

        :raises RuntimeError: If the end of the function is reached this means
            the feedback results have gotten into an invalid state.
        """
        with self.lock:
            try:
                return list(cast(List, self.feedback_list))
            except TypeError:
                # NoneType is not iterable
                # Cache did non exist.
                if self.feedback_list is None:
                    # NoneType missing items/iteritems attr
                    # No results to iterate over.
                    return list()

        # Error out since this case should not be reachable
        raise RuntimeError("Feedback results in an invalid state.")

    def get_positive_adjudication_relevancy(self) -> List[Tuple[DescriptorElement, float]]:
        """
        Return a list of the positively adjudicated descriptors as tuples of
        ``(element, score)`` in order of descending relevancy score.

        This does *not* include external positive adjudications, only
        positively adjudicated descriptors in the working set.

        If refinement has not yet occurred since session creation or the last
        reset, an empty list is returned.

        Cache is invalidated when:
        - A refinement occurs.
        - Positive adjudications change.

        """
        with self.lock:
            try:
                return list(cast(List, self._ordered_pos))
            except TypeError:
                # NoneType is not iterable
                # No cache yet.

                rank_contrib_pos = \
                    self.rank_contrib_pos | self.rank_contrib_pos_ext
                # Results already ordered, so only filter
                r = self._ordered_pos = list(
                    filter(lambda t: t[0] in rank_contrib_pos,
                           self.ordered_results())
                )
                # Shallow copy of the list to protect against external mutation
                return list(r)

    def get_negative_adjudication_relevancy(self) -> List[Tuple[DescriptorElement, float]]:
        """
        Return a list of the negatively adjudicated descriptors as tuples of
        ``(element, score)`` in order of descending relevancy score.

        This does *not* include external negative adjudications, only
        negatively adjudicated descriptors in the working set.

        If refinement has not yet occurred since session creation or the last
        reset, an empty list is returned.

        Cache is invalidated when:
        - A refinement occurs.
        - Negative adjudications change.

        """
        with self.lock:
            try:
                return list(cast(List, self._ordered_neg))
            except TypeError:
                # NoneType is not iterable
                # No cache yet.

                rank_contrib_neg = \
                    self.rank_contrib_neg | self.rank_contrib_neg_ext
                # Results already ordered, so only filter
                r = self._ordered_neg = list(
                    filter(lambda t: t[0] in rank_contrib_neg,
                           self.ordered_results())
                )
                # Shallow copy of the list to protect against external mutation
                return list(r)

    def get_unadjudicated_relevancy(self) -> List[Tuple[DescriptorElement, float]]:
        """
        Return a list of the non-adjudicated descriptor elements as tuples of
        ``(element, score)`` in order of descending relevancy score.

        If refinement has not yet occurred since session creation or the last
        reset, an empty list is returned.

        """
        with self.lock:
            try:
                return list(cast(List, self._ordered_non_adj))
            except TypeError:
                # NoneType is not iterable
                # No cache yet
                pos_and_neg = \
                    self.rank_contrib_pos | self.rank_contrib_pos_ext | \
                    self.rank_contrib_neg | self.rank_contrib_neg_ext

                # Results already ordered, so only filter
                r = self._ordered_non_adj = list(
                    filter(lambda t: t[0] not in pos_and_neg,
                           self.ordered_results())
                )
                # Shallow copy of the list to protect against external mutation
                return list(r)

    def reset(self) -> None:
        """ Reset the IQR Search state

        No positive adjudications, reload original feature data

        """
        with self.lock:
            self.working_set.clear()
            self._wi_seeds_used.clear()
            self.positive_descriptors.clear()
            self.negative_descriptors.clear()
            self.external_positive_descriptors.clear()
            self.external_negative_descriptors.clear()
            self.rank_contrib_pos.clear()
            self.rank_contrib_pos_ext.clear()
            self.rank_contrib_neg.clear()
            self.rank_contrib_neg_ext.clear()

            self.results = None
            self.feedback_list = None
            self._ordered_results = self._ordered_pos = self._ordered_neg = \
                self._ordered_non_adj = None

    ###########################################################################
    # I/O Methods

    # I/O Constants. These should not be changed.
    STATE_ZIP_COMPRESSION = zipfile.ZIP_DEFLATED
    STATE_ZIP_FILENAME = "iqr_state.json"

    def get_state_bytes(self) -> bytes:
        """
        Get a byte representation of the current descriptor and adjudication
        state of this session.

        This does not encode current results or the relevancy index's state, but
        these can be reproduced with this state.

        :return: State representation bytes

        """
        def d_set_to_list(
            d_set: Set[DescriptorElement]
        ) -> List[Tuple[Hashable, str, List[float]]]:
            # Convert set of descriptors to list of tuples:
            #   [..., (uuid, type, vector), ...]
            return [(d.uuid(), d.vector().tolist()) for d in d_set]  # type: ignore

        with self:
            # Convert session descriptors into basic values.
            pos_d = d_set_to_list(self.positive_descriptors)
            neg_d = d_set_to_list(self.negative_descriptors)
            ext_pos_d = d_set_to_list(self.external_positive_descriptors)
            ext_neg_d = d_set_to_list(self.external_negative_descriptors)

        z_buffer = io.BytesIO()
        z = zipfile.ZipFile(z_buffer, 'w', self.STATE_ZIP_COMPRESSION)
        z.writestr(self.STATE_ZIP_FILENAME, json.dumps({
            'pos': pos_d,
            'neg': neg_d,
            'external_pos': ext_pos_d,
            'external_neg': ext_neg_d,
        }))
        z.close()
        return z_buffer.getvalue()

    def set_state_bytes(
        self, b: bytes, descriptor_factory: DescriptorElementFactory
    ) -> None:
        """
        Set this session's state to the given byte representation, resetting
        this session in the process.

        Bytes given must have been retrieved via a previous call to
        ``get_state_bytes`` otherwise this method will fail.

        Since this state may be completely different from the current state,
        this session is reset before applying the new state. Thus, any current
        ranking results are thrown away.

        :param b: Bytes to set this session's state to.
        :param descriptor_factory: Descriptor element factory to use when
            generating descriptor elements from extracted data.

        :raises ValueError: The input bytes could not be loaded due to
            incompatibility.

        """
        z_buffer = io.BytesIO(b)
        z = zipfile.ZipFile(z_buffer, 'r', self.STATE_ZIP_COMPRESSION)
        if self.STATE_ZIP_FILENAME not in z.namelist():
            raise ValueError("Invalid bytes given, did not contain expected "
                             "zipped file name.")

        # Extract expected json file object
        state = json.loads(z.read(self.STATE_ZIP_FILENAME).decode())
        del z, z_buffer

        with self:
            self.reset()

            def load_descriptor(
                _uid: Hashable, vec_list: List[float]
            ) -> DescriptorElement:
                _e = descriptor_factory.new_descriptor(_uid)
                if _e.has_vector():
                    assert _e.vector().tolist() == vec_list, "Found existing vector for UUID '%s' but vectors did not match."  # type: ignore  # noqa: E501
                else:
                    _e.set_vector(np.array(vec_list))
                return _e

            # Read in raw descriptor data from the state, convert to descriptor
            # element, then store in our descriptor sets.
            for source, target in [(state['external_pos'],
                                    self.external_positive_descriptors),
                                   (state['external_neg'],
                                    self.external_negative_descriptors),
                                   (state['pos'], self.positive_descriptors),
                                   (state['neg'], self.negative_descriptors)]:
                for uid, vector_list in source:
                    e = load_descriptor(uid, vector_list)
                    target.add(e)
