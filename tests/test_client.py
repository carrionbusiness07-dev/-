import httpx
import pytest

from minsk_realty.sources.base import PoliteClient, SourceBlocked


def test_backoff_then_success():
    responses = iter([429, 403, 200])
    sleeps = []
    client = PoliteClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(next(responses), text="ok")),
        sleep=sleeps.append,
        backoff_base=10,
    )
    assert client.get("https://example.test/").text == "ok"
    assert sleeps == [10, 20]


def test_three_refusals_in_a_row_stop_the_source():
    client = PoliteClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(403)), sleep=lambda s: None
    )
    with pytest.raises(SourceBlocked):
        client.get("https://example.test/")
