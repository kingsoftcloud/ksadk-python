import httpx
import pytest

from ksadk.skills.service_client import SkillServiceClient


def client(handler, *, kop=True):
    return SkillServiceClient(
        base_url="https://aicp.api.ksyun.com" if kop else "https://skills.example.test",
        region="region-a",
        allow_env_fallback=False,
        transport=httpx.MockTransport(handler),
    )


def page(start, count, total=None):
    data = {
        "Skills": [
            {"SkillId": f"skill-{i}", "VersionId": "version-a", "Name": f"Skill {i}"}
            for i in range(start, start + count)
        ]
    }
    if total is not None:
        data["TotalCount"] = total
    return httpx.Response(200, json={"Data": data})


def test_directory_fetches_skill_101_and_keeps_requested_space():
    requests = []

    def handle(request):
        requests.append(request)
        number = int(request.url.params["PageNumber"])
        return page(0, 100, 101) if number == 1 else page(100, 1, 101)

    listing = client(handle).list_skills_by_space_id("space-a")
    assert listing.skills[-1].skill_id == "skill-100"
    assert len(listing.skills) == 101
    assert listing.truncated is False
    assert listing.next_page is None
    assert [request.url.params["PageNumber"] for request in requests] == ["1", "2"]
    assert all(request.url.params["SpaceId"] == "space-a" for request in requests)


def test_missing_total_fetches_until_a_short_page():
    requests = []

    def handle(request):
        requests.append(request)
        return page(0, 100) if len(requests) == 1 else page(100, 0)

    listing = client(handle).list_skills_by_space_id("space-a")
    assert len(requests) == 2
    assert len(listing.skills) == 100
    assert listing.truncated is False


def test_page_budget_reports_continuation():
    directory = client(lambda _: page(0, 100, 200))
    listing = directory.list_skills_by_space_id("space-a", max_pages=1)
    assert listing.truncated is True
    assert listing.next_page == 2
    assert listing.pagination_warning == "page_budget_exhausted"


def test_repeating_page_is_not_reported_as_complete():
    listing = client(lambda _: page(0, 100, 200)).list_skills_by_space_id("space-a")
    assert len(listing.skills) == 100
    assert listing.truncated is True
    assert listing.pagination_warning == "pagination_repeated"
    assert listing.next_page is None


def test_legacy_rest_preserves_shape_but_reports_missing_entries():
    requests = []

    def handle(request):
        requests.append(request)
        return page(0, 1, 5)

    listing = client(handle, kop=False).list_skills_by_space_id("space-a")
    assert dict(requests[0].url.params) == {"SpaceId": "space-a"}
    assert listing.truncated is True
    assert listing.next_page is None
    assert listing.pagination_warning == "pagination_unavailable"


@pytest.mark.parametrize("kop", [False, True])
def test_directory_cannot_return_another_space(kop):
    directory = client(
        lambda _: httpx.Response(
            200,
            json={
                "Data": {
                    "SkillSpaceId": "space-b",
                    "Skills": [],
                }
            },
        ),
        kop=kop,
    )
    with pytest.raises(ValueError, match="requested space"):
        directory.list_skills_by_space_id("space-a")


def test_changing_total_does_not_claim_consistent_directory():
    requests = []

    def handle(request):
        requests.append(request)
        return page(0, 100, 101) if len(requests) == 1 else page(100, 1, 102)

    listing = client(handle).list_skills_by_space_id("space-a")
    assert listing.truncated is True
    assert listing.pagination_warning == "directory_changed"


@pytest.mark.parametrize("items", [{}, "not-a-list", ["not-an-object"]])
def test_malformed_directory_is_not_empty_success(items):
    directory = client(lambda _: httpx.Response(200, json={"Data": {"Skills": items}}))
    with pytest.raises(ValueError):
        directory.list_skills_by_space_id("space-a")


@pytest.mark.parametrize("data", [None, [], "", False, 0])
def test_malformed_data_envelope_is_not_empty_success(data):
    directory = client(lambda _: httpx.Response(200, json={"Data": data}))
    with pytest.raises(ValueError):
        directory.list_skills_by_space_id("space-a")


def test_partial_directory_requires_explicit_consumer_acknowledgement():
    listing = client(lambda _: page(0, 1, 5), kop=False).list_skills_by_space_id("space-a")
    with pytest.raises(ValueError, match="incomplete"):
        listing.active_skills()
    assert len(listing.active_skills(allow_partial=True)) == 1


def test_public_directory_reports_known_incompleteness():
    listing = client(lambda _: page(0, 1, 5)).list_available_premade_skills()
    assert listing.truncated
    with pytest.raises(ValueError, match="incomplete"):
        listing.active_skills()


def test_runtime_loader_surfaces_partial_directory_without_raw_response(monkeypatch, tmp_path):
    from ksadk.skills.runtime import loader

    directory = client(lambda _: page(0, 1, 5), kop=False)
    monkeypatch.setattr(loader, "SkillServiceClient", lambda **kwargs: directory)
    monkeypatch.setattr(
        loader, "resolve_skill_service_url", lambda **kwargs: "https://skills.example.test"
    )
    monkeypatch.setattr(loader, "load_local_skills", lambda: [])
    monkeypatch.setattr(loader.registry, "user_skill_space_ids", lambda: ["space-a"])
    monkeypatch.setattr(loader.registry, "public_skill_space_ids", lambda: [])
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(tmp_path))
    result = loader.load_skills()
    assert result.skills == []
    assert result.warnings == [
        "Skill directory is incomplete; selection covers only retrieved entries"
    ]
