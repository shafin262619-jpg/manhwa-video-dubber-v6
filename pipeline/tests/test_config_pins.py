"""Pin tests for pipeline.config model constants.

Protect deliberate model choices from being silently changed by a future
agent. See the comments above each constant in pipeline/config.py for the
reasons behind the pins.
"""

import unittest

from pipeline import config


class TTSModelPinTest(unittest.TestCase):
    def test_tts_model_is_pinned_to_flash(self):
        self.assertEqual(
            config.TTS_MODEL,
            "gemini-2.5-flash-preview-tts",
        )


if __name__ == "__main__":
    unittest.main()
