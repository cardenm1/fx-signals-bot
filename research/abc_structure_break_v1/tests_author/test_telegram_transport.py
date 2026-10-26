import argparse
import io
import json
import os
import unittest
from unittest.mock import patch
from abc_author import telegram
from abc_author.notify import run

class TelegramTransportTests(unittest.TestCase):
    def test_transport_payload_and_success_without_network(self):
        with patch.dict(os.environ,{'ABC_AUTHOR_TELEGRAM_BOT_TOKEN':'fake-test-token','ABC_AUTHOR_TELEGRAM_CHAT_ID':'test-chat'}), patch('abc_author.telegram.urllib.request.urlopen') as transport:
            transport.return_value.__enter__.return_value=io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')
            self.assertEqual(telegram.send('test payload'),42)
            req=transport.call_args.args[0]
            self.assertEqual(json.loads(req.data),{'chat_id':'test-chat','text':'test payload'})
            self.assertEqual(transport.call_args.kwargs['timeout'],20)

    def test_api_rejection_is_safe_and_not_retried(self):
        with patch.dict(os.environ,{'ABC_AUTHOR_TELEGRAM_BOT_TOKEN':'fake-test-token','ABC_AUTHOR_TELEGRAM_CHAT_ID':'test-chat'}), patch('abc_author.telegram.urllib.request.urlopen') as transport:
            transport.return_value.__enter__.return_value=io.BytesIO(b'{"ok":false}')
            with self.assertRaisesRegex(RuntimeError,'no automatic retry') as error:telegram.send('test')
            self.assertNotIn('fake-test-token',str(error.exception))
            self.assertEqual(transport.call_count,1)

    def test_missing_secrets_never_calls_transport(self):
        with patch.dict(os.environ,{},clear=True),patch('abc_author.telegram.urllib.request.urlopen') as transport:
            with self.assertRaises(RuntimeError):telegram.send('test')
            transport.assert_not_called()

    def test_manual_test_defaults_to_dry_run(self):
        args=argparse.Namespace(command='test-message',send=False)
        with patch('abc_author.notify.send') as sender:
            run(args)
            sender.assert_not_called()
