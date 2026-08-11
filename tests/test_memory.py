from __future__ import annotations

import json
import os
import unittest
from datetime import date
from unittest.mock import patch

import requests

from src.config import load_config
from src.memory import (
    GitHubGistMemoryClient,
    PlaintextJsonCodec,
    empty_ledger,
    merge_concept_updates,
    relevant_memory_context,
)


class FakeResponse:
    def __init__(self, payload=None, *, text="", status_code=200):
        self.payload = payload
        self.text = text
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.section = load_config()["memory"]

    def test_unconfigured_gist_falls_back_to_empty_memory(self):
        with patch.dict(os.environ, {}, clear=True):
            result = GitHubGistMemoryClient(self.section).read()
        self.assertEqual(result.status, "empty_unconfigured")
        self.assertEqual(result.ledger, empty_ledger())

    def test_network_failure_never_raises(self):
        session = FakeSession([requests.Timeout("timeout")])
        section = dict(self.section, max_retries=1)
        with patch.dict(
            os.environ, {"GIST_ID": "id", "GIST_TOKEN": "token"}, clear=True
        ):
            result = GitHubGistMemoryClient(section, session=session).read()
        self.assertEqual(result.status, "empty_remote_failure")

    def test_reads_and_writes_concept_ledger(self):
        ledger = {
            "schema_version": 1,
            "concepts": {
                "kv_cache": {
                    "name": "KV Cache",
                    "status": "mastered",
                    "mastery_level": 0.9,
                    "mastery_summary": "understood",
                    "source_reports": [],
                }
            },
        }
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "files": {
                            "concept_ledger.json": {
                                "content": json.dumps(ledger),
                                "truncated": False,
                            }
                        }
                    }
                ),
                FakeResponse({}),
            ]
        )
        with patch.dict(
            os.environ, {"GIST_ID": "id", "GIST_TOKEN": "token"}, clear=True
        ):
            client = GitHubGistMemoryClient(self.section, session=session)
            read = client.read()
            write = client.write(read.ledger)
        self.assertEqual(read.status, "available")
        self.assertEqual(read.ledger["concepts"]["kv_cache"]["status"], "mastered")
        self.assertTrue(write.updated)
        body = session.calls[-1][2]["json"]
        self.assertIn("concept_ledger.json", body["files"])

    def test_plaintext_codec_is_replaceable_seam(self):
        codec = PlaintextJsonCodec()
        self.assertEqual(codec.decode(codec.encode(empty_ledger())), empty_ledger())

    def test_future_encryption_codec_fails_closed_instead_of_writing_plaintext(self):
        section = dict(self.section, codec="aes-gcm")
        with patch.dict(
            os.environ, {"GIST_ID": "id", "GIST_TOKEN": "token"}, clear=True
        ):
            client = GitHubGistMemoryClient(section, session=FakeSession([]))
            read = client.read()
            write = client.write(empty_ledger())
        self.assertEqual(read.status, "empty_unsupported_codec")
        self.assertEqual(write.status, "skipped_unsupported_codec")

    def test_memory_context_and_payload_merge(self):
        ledger = empty_ledger()
        context = relevant_memory_context(ledger, ["kv_cache"])
        self.assertEqual(context[0]["status"], "first_contact")
        merged, count = merge_concept_updates(
            ledger,
            {
                "concept_updates": [
                    {
                        "concept_id": "kv-cache",
                        "name": "KV Cache",
                        "status": "learning",
                        "mastery_level": 0.6,
                        "mastery_summary": "理解了分页缓存。",
                    }
                ]
            },
            date(2026, 8, 11),
        )
        self.assertEqual(count, 1)
        self.assertIn("kv_cache", merged["concepts"])


if __name__ == "__main__":
    unittest.main()
