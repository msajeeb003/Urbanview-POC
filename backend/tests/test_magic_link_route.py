"""The magic-link request answers before anything is looked up: the neutral 202 is the same for
every address and does not depend on the lookup, the audit row or the e-mail job, which run after
the response (a failure there is logged, never shown). The language the console was in travels
with the request."""

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
        self.languages: list[str | None] = []

    async def request(self, email: str, language: str | None = None):
        self.calls.append(email)
        self.languages.append(language)
        if self.fail:
            raise RuntimeError("database down")


async def post(service: RecordingService, email: str, **more: str):
    app = make_app(make_settings())
    app.state.magic_link_service = service
    async with make_client(app) as client:
        return await client.post("/v1/auth/magic-link", json={"email": email, **more})


async def test_the_answer_is_neutral_and_the_work_runs_after_it():
    service = RecordingService()
    known = await post(service, "Ana@Example.com")
    unknown = await post(service, "nobody@example.com")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json() == BODY
    assert known.headers["Cache-Control"] == "no-store"
    assert service.calls == ["ana@example.com", "nobody@example.com"]  # both looked up, later
    # the console's language goes with the request; anything but the app's two is a 422
    in_montenegrin = await post(service, "ana@example.com", language="me")
    not_a_language = await post(service, "ana@example.com", language="de")
    assert in_montenegrin.status_code == 202 and in_montenegrin.json() == BODY
    assert not_a_language.status_code == 422
    assert service.languages == [None, None, "me"]


async def test_a_failing_lookup_is_never_shown():
    service = RecordingService(fail=True)
    r = await post(service, "ana@example.com")
    assert r.status_code == 202 and r.json() == BODY
    assert service.calls == ["ana@example.com"]
