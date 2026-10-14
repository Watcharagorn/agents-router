import unittest

from model_catalog import _parse_model_lines


class ModelCatalogTests(unittest.TestCase):
    def test_extracts_exact_ids_from_display_rows(self):
        output = """
        gemini-3.7-flash-high     Gemini 3.7 Flash (High)
        claude-opus-4-6-thinking  Claude Opus 4.6 (Thinking)
        """
        self.assertEqual(
            _parse_model_lines(output),
            ["gemini-3.7-flash-high", "claude-opus-4-6-thinking"],
        )


if __name__ == "__main__":
    unittest.main()
