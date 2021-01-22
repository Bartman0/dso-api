from datetime import date, datetime
from urllib import parse

import pytest
from django.urls import reverse


@pytest.fixture()
def stadsdelen(gebieden_models):
    """
    Create Stadsdeel Zuidoost.
    """
    Stadsdeel = gebieden_models["stadsdelen"]
    stadsdeel = Stadsdeel.objects.create(
        id="03630000000016.1",
        identificatie="03630000000016",
        volgnummer=1,
        registratiedatum=datetime(2006, 6, 12, 5, 40, 12),
        begin_geldigheid=date(2006, 6, 1),
        eind_geldigheid=date(2015, 1, 1),
        naam="Zuidoost",
    )

    stadsdeel_v2 = Stadsdeel.objects.create(
        id="03630000000016.2",
        identificatie="03630000000016",
        volgnummer=2,
        registratiedatum=datetime(2015, 1, 1, 5, 40, 12),
        begin_geldigheid=date(2015, 1, 1),
        eind_geldigheid=None,
        naam="Zuidoost",
    )
    return [stadsdeel, stadsdeel_v2]


@pytest.fixture()
def gebied(gebieden_models, stadsdelen, buurt):
    """
    Creates gebied that is connected to Stadsdeel Zuidoost.
    """
    Gebied = gebieden_models["ggwgebieden"]
    gebied = Gebied.objects.create(
        id="03630950000019.1",
        identificatie="03630950000019",
        volgnummer=1,
        registratiedatum=datetime(2015, 1, 1, 5, 40, 12),
        begin_geldigheid=date(2014, 2, 20),
        naam="Bijlmer-Centrum",
    )
    Gebied.bestaat_uit_buurten.through.objects.create(
        ggwgebieden_id="03630950000019.1", bestaat_uit_buurten_id="03630000000078.1"
    )
    return gebied


@pytest.fixture()
def buurt(gebieden_models, stadsdelen, wijk):
    Buurt = gebieden_models["buurten"]
    return Buurt.objects.create(
        id="03630000000078.1",
        identificatie="03630000000078",
        volgnummer=1,
        code="A00a",
        naam="Kop Zeedijk",
        ligt_in_wijk=wijk,
    )


@pytest.fixture()
def wijk(gebieden_models, stadsdelen):
    Wijk = gebieden_models["wijken"]
    return Wijk.objects.create(
        id="03630012052022.1",
        identificatie="03630012052022",
        volgnummer=1,
        begin_geldigheid=date(2015, 1, 1),
        code="H36",
        naam="Sloterdijk",
        ligt_in_stadsdeel=stadsdelen[1],
    )


@pytest.mark.django_db
class TestViews:
    def test_list_contains_all_objects(self, api_client, stadsdelen):
        """ Prove that default API response contains ALL versions."""
        url = reverse("dynamic_api:gebieden-stadsdelen-list")
        response = api_client.get(url)

        assert response.status_code == 200, response.data
        assert len(response.data["_embedded"]["stadsdelen"]) == 2, response.data["_embedded"][
            "stadsdelen"
        ]

    def test_filtered_list_contains_only_correct_objects(self, api_client, stadsdelen, buurt):
        """Prove that date filter displays only active-on-that-date objects."""
        url = reverse("dynamic_api:gebieden-stadsdelen-list")
        response = api_client.get(f"{url}?geldigOp=2015-01-02")

        assert response.status_code == 200, response.data
        assert len(response.data["_embedded"]["stadsdelen"]) == 1, response.data["_embedded"][
            "stadsdelen"
        ]
        assert response.data["_embedded"]["stadsdelen"][0]["volgnummer"] == 2, response.data[
            "_embedded"
        ]["stadsdelen"][0]

    def test_additionalrelations_works_and_has_temporary_param(
        self, api_client, stadsdelen, wijk, buurt
    ):
        """Prove that the "summary" additionalRelation shows up in the result and
        has a "geldigOp" link.
        """
        url = reverse("dynamic_api:gebieden-wijken-list")
        response = api_client.get(f"{url}?geldigOp=2015-01-02")

        assert response.status_code == 200, response.data
        assert len(response.data["_embedded"]["wijken"]) == 1, response.data["_embedded"]["wijken"]
        assert response.data["_embedded"]["wijken"][0]["volgnummer"] == 1, response.data[
            "_embedded"
        ]["wijken"][0]

        assert response.data["_embedded"]["wijken"][0]["buurt"]["count"] == 1
        href = response.data["_embedded"]["wijken"][0]["buurt"]["href"]
        query_params = parse.parse_qs(parse.urlparse(href).query)
        assert query_params["geldigOp"] == ["2015-01-02"]

    def test_details_record_can_be_requested_by_pk(self, api_client, stadsdelen):
        """ Prove that request with PK (combined field) is allowed."""
        url = reverse("dynamic_api:gebieden-stadsdelen-detail", args=(stadsdelen[0].id,))
        response = api_client.get(url)

        assert response.status_code == 200, response.data
        assert response.data["volgnummer"] == stadsdelen[0].volgnummer, response.data

    def test_details_default_returns_latest_record(self, api_client, stadsdelen):
        """Prove that object can be requested by identification
        and response will contain only latest object."""
        url = reverse("dynamic_api:gebieden-stadsdelen-list")
        response = api_client.get("{}{}/".format(url, stadsdelen[0].identificatie))

        assert response.status_code == 200, response.data
        assert response.data["volgnummer"] == 2, response.data

    def test_details_can_be_requested_with_valid_date(self, api_client, stadsdelen):
        """Prove that object can be requested by identification and date,
        resulting in correct for that date object."""
        url = reverse("dynamic_api:gebieden-stadsdelen-list")
        response = api_client.get(
            "{}{}/?geldigOp=2014-12-12".format(url, stadsdelen[0].identificatie)
        )

        assert response.status_code == 200, response.data
        assert response.data["volgnummer"] == 1, response.data

    def test_details_can_be_requested_with_version(self, api_client, stadsdelen):
        """Prove that object can be requested by identification and version,
        resulting in correct for that version object."""
        url = reverse("dynamic_api:gebieden-stadsdelen-list")
        response = api_client.get("{}{}/?volgnummer=1".format(url, stadsdelen[0].identificatie))

        assert response.status_code == 200, response.data
        assert response.data["volgnummer"] == 1, response.data

    def test_serializer_temporal_request_corrects_link_to_temporal(
        self, api_client, reloadrouter, gebied, buurt
    ):
        """Prove that in case of temporal request links to objects will have request date.
        Allowing follow up date filtering further."""
        url = reverse("dynamic_api:gebieden-ggwgebieden-list")
        # response = api_client.get(url)
        response = api_client.get("{}{}/?geldigOp=2014-05-01".format(url, gebied.id))

        buurt = gebied.bestaat_uit_buurten.all()[0]
        expected_url = "/{}/?geldigOp=2014-05-01".format(buurt.identificatie)
        assert response.data["_links"]["bestaatUitBuurten"][0]["href"].endswith(
            expected_url
        ), response.data["bestaatUitBuurten"]
