"""Regression coverage for WAAPI's WAMP response routing."""
import json
import unittest
from unittest.mock import Mock

from waapi_probe import Waapi, WaapiError


class WampResponseTests(unittest.TestCase):
    def client(self, messages, request_id=7):
        client = Waapi.__new__(Waapi)
        client._next_id = request_id
        client._ws = Mock()
        client._ws.recv_text.side_effect = [json.dumps(item) for item in messages]
        return client

    def test_result_returns_keyword_payload(self):
        client = self.client([[50, 7, {}, [], {"return": [1]}]])
        self.assertEqual(client.call("test.get"), {"return": [1]})

    def test_call_error_preserves_uri_args_and_kwargs(self):
        client = self.client([
            [8, 48, 7, {}, "ak.wwise.invalid_arguments", ["bad"], {"message": "Invalid field"}]
        ])
        with self.assertRaises(WaapiError) as caught:
            client.call("test.get")
        self.assertEqual(caught.exception.uri, "test.get")
        self.assertEqual(caught.exception.payload, {
            "error": "ak.wwise.invalid_arguments",
            "args": ["bad"],
            "kwargs": {"message": "Invalid field"},
        })

    def test_error_without_optional_arguments(self):
        client = self.client([[8, 48, 7, {}, "wamp.error.no_such_procedure"]])
        with self.assertRaises(WaapiError) as caught:
            client.call("test.missing")
        self.assertEqual(caught.exception.payload, {"error": "wamp.error.no_such_procedure"})

    def test_unrelated_error_is_ignored_even_when_request_id_is_48(self):
        client = self.client([
            [8, 48, 9, {}, "unrelated.call"],
            [8, 32, 48, {}, "unrelated.subscribe"],
            [50, 48, {}, [], {"ok": True}],
        ], request_id=48)
        self.assertEqual(client.call("test.get"), {"ok": True})


if __name__ == "__main__":
    unittest.main()
