import warnings
from typing import Callable, Iterable, List, Optional

from django.core.paginator import EmptyPage
from django.core.paginator import Page as DjangoPage
from django.core.paginator import PageNotAnInteger
from django.core.paginator import Paginator as DjangoPaginator
from django.db.models.query import QuerySet
from django.http import Http404
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from rest_framework_dso.embedding import ObservableIterator


class DSOPaginator(DjangoPaginator):
    """A paginator that supports streaming.

    This paginator avoids expensive count queries.
    So num_pages() is not supported.
    """

    def __init__(self, object_list, per_page, orphans=0, allow_empty_first_page=True):
        if orphans != 0:
            warnings.warn(
                "DSOPaginator instantiated with non-zero value in orphans. \
                    Orphans are not supported by this class and will be ignored.",
                RuntimeWarning,
            )
        super().__init__(object_list, per_page, 0, allow_empty_first_page)

    def validate_number(self, number):
        """Validate the given 1-based page number."""
        try:
            if isinstance(number, float) and not number.is_integer():
                raise ValueError
            number = int(number)
        except (TypeError, ValueError) as e:
            raise PageNotAnInteger(_("That page number is not an integer")) from e
        if number < 1:
            raise EmptyPage(_("That page number is less than 1"))
        return number

    def get_page(self, number):
        """
        Return a valid page, even if the page argument isn't a number or isn't
        in range.
        """
        try:
            number = self.validate_number(number)
        except PageNotAnInteger:
            number = 1
        return self.page(number)

    def page(self, number):
        """Return a Page object for the given 1-based page number."""
        number = self.validate_number(number)
        bottom = (number - 1) * self.per_page
        top = bottom + self.per_page

        # One additional sentinel object, is given to the page.
        # This object should not be rendered, but it allows the page
        # to detect whether more items exist beyond it and hence wether a next page exists.
        sentinel = 1
        return self._get_page(self.object_list[bottom : top + sentinel], number, self)

    def _get_page(self, *args, **kwargs):
        """
        Return an instance of a single page.

        This hook can be used by subclasses to use an alternative to the
        standard :cls:`Page` object.
        """
        return DSOPage(*args, **kwargs)

    @cached_property
    def num_pages(self):
        """Total number of pages is unknown."""
        raise NotImplementedError(
            "DSOPaginator does not support method num_pages. The number of pages is unknown."
        )

    @property
    def page_range(self):
        """
        Page Range not supported.
        """
        raise NotImplementedError(
            "DSOPaginator does not support method page_range. The number of pages is unknown."
        )


class ObservableQuerySet(QuerySet):
    """A QuerySet that has observable iterators.

    This class overloads the iterator and __iter__ methods
    and wraps the iterators returned by the base class
    in ObservableIterators.

    Observers added to an instance will be notified of
    iteration events on the last iterator created by
    the instance.
    """

    def __init__(self, *args, **kwargs):
        self._item_callbacks: list[Callable] = []
        self._obs_iterator: ObservableIterator = None
        super().__init__(*args, **kwargs)

    @classmethod
    def from_queryset(cls, queryset: QuerySet, observers: List[Callable] = None):
        """Turn a QuerySet instance into an ObservableQuerySet"""
        queryset.__class__ = ObservableQuerySet
        queryset._item_callbacks = list(observers) if observers else []
        queryset._obs_iterator = None
        return queryset

    def iterator(self, *args, **kwargs):
        """Return observable iterator and add observer.
        Wraps an observable iterator around the iterator
        returned by the base class.
        """
        iterator = super().iterator(*args, **kwargs)
        return self._set_observable_iterator(iterator)

    def __iter__(self):
        """Return observable iterator and add observer.
        Wraps an observable iterator around the iterator
        returned by the base class.
        """
        return self._set_observable_iterator(super().__iter__())

    def _set_observable_iterator(self, iterator: Iterable) -> ObservableIterator:
        """Wrap an iterator inside an ObservableIterator"""
        iterator = ObservableIterator(iterator)
        iterator.add_observer(self._item_observer)

        # Remove observer from existing oberservable iterator
        if self._obs_iterator is not None:
            self._obs_iterator.clear_observers()

        self._obs_iterator = iterator

        # Notify observers of empty iterator
        if not iterator:
            self._item_observer(None, True)

        return iterator

    def add_observer(self, callback: Callable):
        """Install an observer callback that is notified when items are iterated"""
        self._item_callbacks.append(callback)

    def _item_observer(self, item, is_empty=False):
        """Notify all observers."""
        for callback in self._item_callbacks:
            callback(item, self._obs_iterator, is_empty)

    def is_iterated(self) -> bool:
        """The iterator has finished"""
        return self._obs_iterator is not None and self._obs_iterator._is_iterated


class DSOPage(DjangoPage):
    """A page that can be streamed.

    This page avoids count queries by delaying calculation of the page length.

    The number of items on the page cannot be known before
    the object_list has been iterated.
    Therefore __len__ and has_next methods are only valid
    after the object list has been iterated.
    """

    def __init__(self, object_list, number, paginator):
        self.number = number
        self.paginator = paginator
        self._length = 0
        self._has_next = None
        if isinstance(object_list, QuerySet):
            # We have to cast the queryset instance into an observable queryset here. Not pretty.
            # Pagination in DRF is handled early on in the pipeline where, because we stream the
            # data we don't know the number of items on the page.
            # We need the number of items to tell wether a next page exists.
            #
            # So we need to keep track of the iteration process happening in the renderer.
            # We can not create the iterator at this point, because streaming will break
            # further down the line if object_list is not a queryset
            # And we dont want to move all the pagination logic into the generic renderer.
            # So in order to watch the iterators created later on by the queryset we have to
            # wrap it here.
            self.object_list = ObservableQuerySet.from_queryset(
                object_list, [self._watch_object_list]
            )
        else:
            self.object_list = ObservableIterator(object_list, [self._watch_object_list])
            # Object list is empty
            if not self.object_list:
                self._watch_object_list(None, None, True)

    def __repr__(self):
        return "<Page %s>" % (self.number)

    def __len__(self):
        return self._length

    def has_next(self) -> Optional[bool]:
        """There is a page after this one.
        Returns:
            True, if a next page exist.
            False, if a next page does not exist.
            None, if unknown.
        """
        if self.is_iterated():
            return self._has_next
        else:
            return None

    def is_iterated(self) -> bool:
        """Check whether the all objects on this page have been iterated,
        so the number of items on the page is known.
        """
        return self.object_list.is_iterated()

    def _watch_object_list(
        self, item, observable_iterator: ObservableIterator = None, iterator_is_empty=False
    ):
        """Adjust page length and throw away the sentinel item"""

        # Observable queryset returns the iterator
        # Observable iterator does not
        if observable_iterator is None:
            observable_iterator = self.object_list

        # If this is not page 1 and the object list is empty
        # user navigated beyond the last page so we throw a 404.
        if iterator_is_empty:
            if self.number > 1:
                raise Http404()

        # Set the number of objects read up till now
        number_returned = observable_iterator.number_returned
        self._length = min(self.paginator.per_page, number_returned)

        # If the sentinel item was returned a next page exists
        self._has_next = number_returned > self.paginator.per_page

        # The page was passed an extra object in its object_list
        # as a sentinel to detect wether more items exist beyond this page
        # and hence a next page exists.
        # This object should not be rendered so we call next() again to stop the iterator.
        if observable_iterator.number_returned == self.paginator.per_page:
            # Throw away the sentinel item
            next(observable_iterator)