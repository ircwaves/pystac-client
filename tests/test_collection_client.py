import pytest

from pystac_client import CollectionClient
from pystac_client.client import Client

from .helpers import STAC_URLS


class TestCollectionClient:

    @pytest.mark.vcr
    def test_instance(self):
        client = Client.open(STAC_URLS['PLANETARY-COMPUTER'])
        collection = client.get_collection('aster-l1t')

        assert isinstance(collection, CollectionClient)
        assert str(collection) == '<CollectionClient id=aster-l1t>'

    # def test_get_items(self):
