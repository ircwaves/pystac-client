import json
import logging
from copy import deepcopy
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)
from typing_extensions import TypeAlias
from urllib.parse import urlparse
import warnings

import pystac
from pystac.link import Link
from pystac.serialization import (
    identify_stac_object,
    identify_stac_object_type,
    merge_common_properties,
    migrate_to_latest,
)
from pystac.stac_io import DefaultStacIO
from requests import Request, Session

import pystac_client

from .exceptions import APIError

if TYPE_CHECKING:
    from pystac.catalog import Catalog as Catalog_Type
    from pystac.stac_object import STACObject as STACObject_Type

logger = logging.getLogger(__name__)


Timeout: TypeAlias = Optional[Union[float, Tuple[float, float], Tuple[float, None]]]


class StacApiIO(DefaultStacIO):
    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        conformance: Optional[List[str]] = None,
        parameters: Optional[Dict[str, Any]] = None,
        request_modifier: Optional[Callable[[Request], Union[Request, None]]] = None,
        timeout: Timeout = None,
    ):
        """Initialize class for API IO

        Args:
            headers : Optional dictionary of headers to include in all requests
            conformance (DEPRECATED) : Optional list of `Conformance Classes
                <https://github.com/radiantearth/stac-api-spec/blob/master/overview.md#conformance-classes>`__.

                .. deprecated:: 0.7.0
                    Conformance can be altered on the client class directly

            parameters: Optional dictionary of query string parameters to
              include in all requests.
            request_modifier: Optional callable that can be used to modify Request
              objects before they are sent. If provided, the callable receives a
              `request.Request` and must either modify the object directly or return
              a new / modified request instance.
            timeout: Optional float or (float, float) tuple following the semantics
              defined by `Requests
              <https://requests.readthedocs.io/en/latest/api/#main-interface>`__.

        Return:
            StacApiIO : StacApiIO instance
        """
        # TODO - this should super() to parent class

        if conformance is not None:
            warnings.warn(
                (
                    "The `conformance` option is deprecated and will be "
                    "removed in the next major release. Instead use "
                    "`Client.set_conforms_to` or `Client.add_conforms_to` to control "
                    "behavior."
                ),
                category=FutureWarning,
            )

        self.session = Session()
        self.timeout = timeout
        self.update(
            headers=headers, parameters=parameters, request_modifier=request_modifier
        )

    def update(
        self,
        headers: Optional[Dict[str, str]] = None,
        parameters: Optional[Dict[str, Any]] = None,
        request_modifier: Optional[Callable[[Request], Union[Request, None]]] = None,
        timeout: Timeout = None,
    ) -> None:
        """Updates this StacApi's headers, parameters, and/or request_modifer.

        Args:
            headers : Optional dictionary of headers to include in all requests
            parameters: Optional dictionary of query string parameters to
              include in all requests.
            request_modifier: Optional callable that can be used to modify Request
              objects before they are sent. If provided, the callable receives a
              `request.Request` and must either modify the object directly or return
              a new / modified request instance.
            timeout: Optional float or (float, float) tuple following the semantics
              defined by `Requests
              <https://requests.readthedocs.io/en/latest/api/#main-interface>`__.
        """
        self.session.headers.update(headers or {})
        self.session.params.update(parameters or {})  # type: ignore
        self._req_modifier = request_modifier
        self.timeout = timeout

    def read_text(self, source: pystac.link.HREF, *args: Any, **kwargs: Any) -> str:
        """Read text from the given URI.

        Overwrites the default method for reading text from a URL or file to allow
        :class:`urllib.request.Request` instances as input. This method also raises
        any :exc:`urllib.error.HTTPError` exceptions rather than catching
        them to allow us to handle different response status codes as needed.
        """
        if isinstance(source, Link):
            link = source.to_dict()
            href = link["href"]
            # get headers and body from Link and add to request from simple STAC
            # resolver
            merge = bool(link.get("merge", False))

            # If the link object includes a "method" property, use that. If not
            # fall back to 'GET'.
            method = link.get("method", "GET")
            # If the link object includes a "headers" property, use that and
            # respect the "merge" property.
            headers = link.get("headers", None)

            # If "POST" use the body object that and respect the "merge" property.
            link_body = link.get("body", {})
            if method == "POST":
                parameters = (
                    {**(kwargs.get("parameters", {})), **link_body}
                    if merge
                    else link_body
                )
            else:
                # parameters are already in the link href
                parameters = {}

            return self.request(
                href, method=method, headers=headers, parameters=parameters
            )
        else:  # str or something that can be str'ed
            href = str(source)
            if _is_url(href):
                return self.request(href, *args, **kwargs)
            else:
                with open(href) as f:
                    href_contents = f.read()
                return href_contents

    def request(
        self,
        href: str,
        method: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Makes a request to an http endpoint

        Args:
            href (str): The request URL
            method (Optional[str], optional): The http method to use, 'GET' or 'POST'.
              Defaults to None, which will result in 'GET' being used.
            headers (Optional[Dict[str, str]], optional): Additional headers to include
                in request. Defaults to None.
            parameters (Optional[Dict[str, Any]], optional): parameters to send with
                request. Defaults to None.

        Raises:
            APIError: raised if the server returns an error response

        Return:
            str: The decoded response from the endpoint
        """
        if method == "POST":
            request = Request(method=method, url=href, headers=headers, json=parameters)
        else:
            params = deepcopy(parameters) or {}
            request = Request(method="GET", url=href, headers=headers, params=params)
        try:
            modified = self._req_modifier(request) if self._req_modifier else None
            prepped = self.session.prepare_request(modified or request)
            msg = f"{prepped.method} {prepped.url} Headers: {prepped.headers}"
            if method == "POST":
                msg += f" Payload: {json.dumps(request.json)}"
            if self.timeout is not None:
                msg += f" Timeout: {self.timeout}"
            logger.debug(msg)
            resp = self.session.send(prepped, timeout=self.timeout)
        except Exception as err:
            logger.debug(err)
            raise APIError(str(err))
        if resp.status_code != 200:
            raise APIError.from_response(resp)
        try:
            return resp.content.decode("utf-8")
        except Exception as err:
            raise APIError(str(err))

    def write_text_to_href(self, href: str, *args: Any, **kwargs: Any) -> None:
        if _is_url(href):
            raise APIError("Transactions not supported")
        else:
            return super().write_text_to_href(href, *args, **kwargs)

    def stac_object_from_dict(
        self,
        d: Dict[str, Any],
        href: Optional[pystac.link.HREF] = None,
        root: Optional["Catalog_Type"] = None,
        preserve_dict: bool = True,
    ) -> "STACObject_Type":
        """Deserializes a :class:`~pystac.STACObject` sub-class instance from a
        dictionary.

        Args:
            d : The dictionary to deserialize
            href : Optional href to associate with the STAC object
            root : Optional root :class:`~pystac.Catalog` to associate with the
                STAC object.
            preserve_dict: If ``False``, the dict parameter ``d`` may be modified
                during this method call. Otherwise the dict is not mutated.
                Defaults to ``True``, which results results in a deepcopy of the
                parameter. Set to ``False`` when possible to avoid the performance
                hit of a deepcopy.
        """
        if identify_stac_object_type(d) == pystac.STACObjectType.ITEM:
            collection_cache = None
            if root is not None:
                collection_cache = root._resolved_objects.as_collection_cache()

            # Merge common properties in case this is an older STAC object.
            merge_common_properties(
                d, json_href=str(href), collection_cache=collection_cache
            )

        info = identify_stac_object(d)
        d = migrate_to_latest(d, info)

        if info.object_type == pystac.STACObjectType.CATALOG:
            result = pystac_client.client.Client.from_dict(
                d, href=str(href), root=root, migrate=False, preserve_dict=preserve_dict
            )
            result._stac_io = self
            return result

        if info.object_type == pystac.STACObjectType.COLLECTION:
            assert isinstance(root, pystac_client.client.Client)
            return pystac_client.collection_client.CollectionClient.from_dict(
                d, href=str(href), root=root, migrate=False, preserve_dict=preserve_dict
            )

        if info.object_type == pystac.STACObjectType.ITEM:
            return pystac.Item.from_dict(
                d, href=str(href), root=root, migrate=False, preserve_dict=preserve_dict
            )

        raise ValueError(f"Unknown STAC object type {info.object_type}")

    def get_pages(
        self,
        url: str,
        method: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Iterator that yields dictionaries for each page at a STAC paging
        endpoint, e.g., /collections, /search

        Return:
            Dict[str, Any] : JSON content from a single page
        """
        page = self.read_json(url, method=method, parameters=parameters)
        if not (page.get("features") or page.get("collections")):
            return None
        yield page

        next_link = next(
            (link for link in page.get("links", []) if link["rel"] == "next"), None
        )
        while next_link:
            link = Link.from_dict(next_link)
            page = self.read_json(link, parameters=parameters)
            if not (page.get("features") or page.get("collections")):
                return None
            yield page

            # get the next link and make the next request
            next_link = next(
                (link for link in page.get("links", []) if link["rel"] == "next"), None
            )


def _is_url(href: str) -> bool:
    url = urlparse(href)
    return bool(url.scheme) and bool(url.netloc)
