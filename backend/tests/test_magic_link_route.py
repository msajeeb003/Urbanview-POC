"""The magic-link request answers before anything is looked up: the neutral 202 is the same for
every address and does not depend on the lookup, the audit row or the e-mail job, which run after
the response (a failure there is logged, never shown)."""

from __future__ import annotations

from tests.helpers import make_app, make_client, make_settings

BODY = {
    "status": "accepted",
    "message_en": "If this address belongs to a staff account, a login link is on its way.",
    "message_me": "Ako ova adresa pripada nalogu osoblja, link za prijavu je poslat.",
}


class RecordingService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def request(self, email: str):
        self.calls.append(email)
        if self.fail:
            raise RuntimeError("database down")


async def post(service: RecordingService, email: str):
    app = make_app(make_settings())
    app.state.magic_link_service = service
    async with make_client(app) as client:
        return await client.post("/v1/auth/magic-link", json={"email": email})


async def test_the_answer_is_neutral_and_the_work_runs_after_it():
    service = RecordingService()
    known = await post(service, "Ana@Example.com")
    unknown = await post(service, "nobody@example.com")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json() == BODY
    assert known.headers["Cache-Control"] == "no-store"
    assert service.calls == ["ana@example.com", "nobody@example.com"]  # both looked up, later


async def test_a_failing_lookup_is_never_shown():
    service = RecordingService(fail=True)
    r = await post(service, "ana@example.com")
    assert r.status_code == 202 and r.json() == BODY
    assert service.calls == ["ana@example.com"]
