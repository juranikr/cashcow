import unittest

from browser_planner import BrowseTrace, next_public_read_action, ranked_public_read_actions


def observation(**overrides):
    payload = {
        "url": "https://example.com/docs",
        "title": "API 개요",
        "renderedText": "짧은 소개",
        "ariaSnapshot": "heading: API 개요",
        "domHash": "hash-1",
        "interactiveElements": [
            {
                "ref": "cc-1", "tag": "a", "role": "link", "accessibleName": "로그인",
                "href": "https://example.com/login", "enabled": True, "download": False,
            },
            {
                "ref": "cc-2", "tag": "a", "role": "link", "accessibleName": "API 인증 가이드",
                "href": "https://example.com/docs/auth", "enabled": True, "download": False,
            },
            {
                "ref": "cc-3", "tag": "summary", "role": "", "accessibleName": "요청 예시 펼치기",
                "href": "", "enabled": True, "disclosureKind": "details", "expanded": "false",
            },
            {
                "ref": "cc-4", "tag": "input", "type": "search", "name": "q", "formMethod": "get",
                "accessibleName": "문서 검색", "enabled": True, "placeholder": "검색",
            },
        ],
    }
    payload.update(overrides)
    return payload


class BrowserPlannerTests(unittest.TestCase):
    def test_first_step_does_not_quit_before_reading(self):
        action = next_public_read_action(
            observation(), "https://example.com/docs 에서 API 인증을 살펴봐", 1, BrowseTrace(),
            {"conscientiousness": 0.95},
        )
        self.assertNotEqual(action["action"], "done")

    def test_follows_related_public_link_instead_of_login(self):
        ranked = ranked_public_read_actions(
            observation(renderedText="API 개요 문서입니다. " * 20),
            "API 인증 가이드를 열어 내용을 확인해줘",
            2,
            BrowseTrace(),
            {"conscientiousness": 0.95},
        )
        actions = [(item["action"], item.get("ref")) for item in ranked]
        self.assertIn(("follow_link", "cc-2"), actions)
        self.assertNotIn(("follow_link", "cc-1"), actions)

    def test_uses_public_get_search_when_command_asks_to_search(self):
        action = next_public_read_action(
            observation(), "이 문서에서 인증 헤더를 검색해줘", 1, BrowseTrace(),
            {"conscientiousness": 0.9},
        )
        self.assertEqual(action["action"], "search")
        self.assertEqual(action["ref"], "cc-4")
        self.assertIn("인증", action["value"])

    def test_skips_unchanged_control_and_keeps_working(self):
        trace = BrowseTrace()
        first = next_public_read_action(
            observation(), "요청 예시와 API 인증을 살펴봐", 1, trace, {"conscientiousness": 0.94},
        )
        self.assertIn(first["action"], {"click_disclosure", "follow_link", "scroll", "search"})
        same_page = observation()
        same_page["domHash"] = "hash-1"
        second = next_public_read_action(
            same_page, "요청 예시와 API 인증을 살펴봐", 2, trace, {"conscientiousness": 0.94},
        )
        if first.get("ref"):
            self.assertNotEqual(second.get("ref"), first.get("ref"))


if __name__ == "__main__":
    unittest.main()
