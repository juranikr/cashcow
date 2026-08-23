import tempfile
import unittest
from pathlib import Path

from engine import WorldEngine
from storage import FileStore


def result(outcome: str):
    return {
        "reply": "검증 응답",
        "publicDecision": {
            "observations": ["도구 결과 확인"], "objective": "완료 조건 충족",
            "chosenAction": "결과 검증", "rationale": "근거 확인",
            "alternatives": [], "evidenceRefs": ["test"], "blockers": [], "confidence": 0.9,
        },
        "toolRuns": [{"tool": "test", "input": "request", "output": "response"}],
        "collaboration": [], "registerKnowledge": False,
        "report": {
            "title": "테스트 리포트", "outcome": outcome, "summary": f"{outcome} 결과",
            "body": "검증 본문", "limitations": ["테스트 한계"] if outcome != "completed" else [],
            "lessons": ["완료 조건을 재검증한다."],
        },
    }


class PersistentEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "world.json")
        self.store = FileStore(self.path)
        await self.store.initialize()
        self.engine = WorldEngine(self.store)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_player_job_and_idempotency_survive_reopen(self):
        snapshot = await self.engine.snapshot("owner-1")
        self.assertEqual(snapshot["player"]["position"], {"x": 49.0, "y": 54.0})
        moved = await self.engine.move_player("owner-1", {"x": 50.0, "y": 54.0}, "east")
        self.assertEqual(moved["facing"], "east")

        first = await self.engine.enqueue("harin", "공용 정보를 검색하고 등록해줘", "owner-1", "same-command-001")
        second = await self.engine.enqueue("harin", "공용 정보를 검색하고 등록해줘", "owner-1", "same-command-001")
        self.assertEqual(first["jobId"], second["jobId"])

        reopened = WorldEngine(FileStore(self.path))
        await reopened.store.initialize()
        recovered = await reopened.snapshot("owner-1")
        self.assertEqual(recovered["player"]["position"], moved["position"])
        detail = await reopened.job_detail(first["jobId"])
        self.assertEqual(detail["job"]["status"], "active")

    async def test_partial_replans_and_terminal_failure_releases_tool(self):
        queued = await self.engine.enqueue("harin", "공용 정보를 검색하고 등록해줘", "owner-2", "partial-command-001")
        job_id = queued["jobId"]

        await self.store.mutate(lambda data: data["jobs"][job_id].update({"attempt": 1, "phase": "executing"}))
        await self.engine._complete_job(job_id, result("partial"))
        detail = await self.engine.job_detail(job_id)
        self.assertEqual(detail["job"]["status"], "active")
        self.assertEqual(detail["job"]["phase"], "working")
        self.assertEqual(len(detail["job"]["attemptResults"]), 1)

        await self.store.mutate(lambda data: data["jobs"][job_id].update({"attempt": 3, "phase": "executing"}))
        await self.engine._complete_job(job_id, result("failed"))
        detail = await self.engine.job_detail(job_id)
        self.assertEqual(detail["job"]["status"], "failed")
        self.assertEqual(detail["job"]["plan"][-1]["status"], "blocked")
        snapshot = await self.store.snapshot()
        self.assertNotIn("pc-03", snapshot["world"]["toolOccupancies"])
        self.assertEqual(snapshot["reports"][-1]["outcome"], "failed")

    async def test_board_versions_keep_pixel_text_and_original_author(self):
        snapshot = await self.engine.snapshot("owner-3")
        board = snapshot["whiteboard"]
        existing = dict(board["textBlocks"][0])
        existing["text"] = "대표가 내용을 수정"
        new_block = {
            "id": "owner-note", "x": 30, "y": 80, "width": 90, "height": 40,
            "text": "새 메모", "color": "#26394f", "background": "#f9f4dfef", "fontSize": 9,
        }
        saved = await self.engine.save_board({
            "title": "테스트", "palette": board["palette"], "pixelsBase64": board["pixelsBase64"],
            "textBlocks": [existing, new_block], "expectedVersion": board["version"], "changeSummary": "테스트 저장",
        }, "owner-3", "대표")
        self.assertEqual(saved["textBlocks"][0]["authorName"], "시스템")
        self.assertEqual(saved["textBlocks"][1]["authorName"], "대표")
        previous = await self.engine.board_revision(1)
        current = await self.engine.board_revision(2)
        self.assertNotEqual(previous["textBlocks"], current["textBlocks"])
        self.assertEqual(len(current["pixelsBase64"]), len(previous["pixelsBase64"]))


if __name__ == "__main__":
    unittest.main()
