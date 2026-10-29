import json
import unittest

from hive.mqtt.ota_qr_prep import (
    OTA_CONFIG_RECORD, OTA_IMAGE_URL, OTA_URL_ENDPOINT, OTA_VERSION,
    build_ota_endpoint_qr_data, decode_ota_endpoint_qr_data,
    expected_robot_request_url,
)
from hive.mqtt.ota_monitor import classify_log_line, post_ota_checks


class TestOtaQrPrep(unittest.TestCase):
    def test_qr_roundtrip(self):
        qr = build_ota_endpoint_qr_data()
        obj = json.loads(qr)
        self.assertEqual(obj["debug"]["command"], "om")
        fields = decode_ota_endpoint_qr_data(qr)
        self.assertEqual(fields["webservice_root"], "https://duranaki.com/")
        self.assertEqual(fields["gcp_project"], "o")
        self.assertEqual(fields["mqtt_host"], "192.168.4.31")
        self.assertEqual(fields["override_port"], 8883)
        self.assertTrue(fields["disable_verify"])

    def test_qr_rejects_bad_payload(self):
        with self.assertRaises(ValueError):
            decode_ota_endpoint_qr_data(json.dumps({"debug": {"command": "x", "param": "AA=="}}))
        with self.assertRaises(ValueError):
            decode_ota_endpoint_qr_data("not json")

    def test_config_and_urls(self):
        self.assertEqual(OTA_CONFIG_RECORD,
                         {"ota_update": {"id": "rls", "version": OTA_VERSION}})
        self.assertTrue(OTA_VERSION.endswith("-v24.10.803-rls-robot"))
        self.assertEqual(expected_robot_request_url(),
                         OTA_URL_ENDPOINT + "?access_token=notoken")
        self.assertTrue(OTA_IMAGE_URL.startswith("https://duranaki.com/moxie/"))


class TestOtaMonitor(unittest.TestCase):
    def test_classify(self):
        stages, _ = classify_log_line("Sending HTTP TOKEN to device d_x")
        self.assertIn("token_request", stages)
        _, failures = classify_log_line("OpenSSL Error: tlsv1 alert unknown ca")
        self.assertIn("tlsv1 alert", failures)

    def test_post_checks_mention_803_and_pairing(self):
        text = " ".join(post_ota_checks())
        self.assertIn("803", text)
        self.assertIn("embodied_robot_id", text)


if __name__ == "__main__":
    unittest.main()
