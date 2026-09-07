import unittest

from src.investigator import _RESOURCE_LIMIT_MESSAGE, _failure_error_text


class FailureErrorTextTest(unittest.TestCase):
    def test_api_error_does_not_surface_success_subtype(self) -> None:
        self.assertEqual(
            _failure_error_text("success", "api_error"),
            "排查失败（api_error），请联系管理员查看日志",
        )

    def test_real_error_subtype_remains_the_reason(self) -> None:
        self.assertEqual(
            _failure_error_text("error_during_execution", "tool_error"),
            "排查失败（error_during_execution），请联系管理员查看日志",
        )

    def test_resource_limit_keeps_the_friendly_message(self) -> None:
        self.assertEqual(
            _failure_error_text("error_max_turns", "max_turns"),
            _RESOURCE_LIMIT_MESSAGE,
        )

    def test_missing_reason_is_explicit(self) -> None:
        self.assertEqual(
            _failure_error_text(None, None),
            "排查失败（未知原因），请联系管理员查看日志",
        )


if __name__ == "__main__":
    unittest.main()
