import tempfile
import unittest
from pathlib import Path

from engine import WorldEngine, _command_intent, _evidence_verification, _relevant_knowledge
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


def runtime_status():
    return {
        "evidenceId": "runtime-status-test", "source": "cashcow-runtime",
        "execution": {"runnerActive": True, "runnerHealthy": True, "browserConnectionRequired": False, "paused": False},
        "persistence": {"durableStorageConfigured": True, "backend": "dynamodb", "loadedExistingState": True},
        "platform": {"verified": True, "launchType": "FARGATE", "knownStatus": "RUNNING"},
        "state": {"failureAudit": {"observedJobCount": 0, "recentFailures": []}},
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
        public = await self.engine.snapshot("owner-2")
        harin = next(item for item in public["agents"] if item["id"] == "harin")
        self.assertNotIn("activeJob", harin)
        self.assertEqual(harin["activity"], "다음 요청을 기다리는 중")
        self.assertEqual(harin["progress"], 0)

    async def test_startup_recovery_clears_stale_terminal_agent_state_at_any_progress(self):
        def make_stale(data):
            harin = next(item for item in data["world"]["agents"] if item["id"] == "harin")
            harin.update({
                "activeJobId": "missing-terminal-job", "activity": "컴퓨터에서 실제 작업 중",
                "focus": "종료된 작업", "progress": 64, "currentTool": "computer", "toolStation": "pc-03",
            })
            data["world"]["toolOccupancies"]["pc-03"] = {"jobId": "missing-terminal-job", "agentId": "harin"}
        await self.store.mutate(make_stale)
        await self.engine._recover_executions()
        snapshot = await self.store.snapshot()
        harin = next(item for item in snapshot["world"]["agents"] if item["id"] == "harin")
        self.assertIsNone(harin["activeJobId"])
        self.assertIsNone(harin["currentTool"])
        self.assertEqual(harin["progress"], 0)
        self.assertNotIn("pc-03", snapshot["world"]["toolOccupancies"])

    async def test_verified_knowledge_can_be_invalidated_without_losing_audit_history(self):
        queued = await self.engine.enqueue("harin", "공용 정보에 운영 검증 결과를 등록해줘", "owner-4", "invalidate-command-001")
        job_id = queued["jobId"]
        completed = result("completed")
        completed.update({
            "registerKnowledge": True,
            "verification": {
                "status": "verified", "passed": True, "requiredChecks": ["test"],
                "evidenceIds": ["evidence-test"], "issues": [],
            },
        })
        await self.store.mutate(lambda data: data["jobs"][job_id].update({"attempt": 1, "phase": "executing"}))
        await self.engine._complete_job(job_id, completed)
        before = await self.store.snapshot()
        report_id = next(item["id"] for item in before["reports"] if item["planId"] == job_id)
        harin_before = next(item for item in before["world"]["agents"] if item["id"] == "harin")
        self.assertEqual(harin_before["score"], 84)
        self.assertEqual(before["knowledge"][0]["status"], "active")

        invalidated = await self.engine.invalidate_job_artifacts(job_id, "무관한 외부 자료를 내부 운영 근거로 사용함", "owner-4")
        self.assertFalse(invalidated["alreadyInvalidated"])
        after = await self.store.snapshot()
        self.assertEqual(after["jobs"][job_id]["status"], "failed")
        self.assertEqual(next(item for item in after["reports"] if item["id"] == report_id)["verificationStatus"], "invalidated")
        self.assertEqual(after["knowledge"][0]["status"], "invalidated")
        harin_after = next(item for item in after["world"]["agents"] if item["id"] == "harin")
        self.assertEqual(harin_after["score"], 83)
        self.assertEqual(harin_after["memories"][-1]["verificationStatus"], "invalidated")

        reopened = FileStore(self.path)
        await reopened.initialize()
        persisted = await reopened.snapshot()
        self.assertEqual(persisted["knowledge"][0]["status"], "invalidated")
        reopened_engine = WorldEngine(reopened)
        again = await reopened_engine.invalidate_job_artifacts(job_id, "같은 사유", "owner-4")
        self.assertTrue(again["alreadyInvalidated"])
        persisted_again = await reopened.snapshot()
        self.assertEqual(next(item for item in persisted_again["world"]["agents"] if item["id"] == "harin")["score"], 83)

    async def test_invalidation_reverses_only_the_score_actually_awarded(self):
        await self.store.mutate(lambda data: next(item for item in data["world"]["agents"] if item["id"] == "harin").update({"score": 100}))
        queued = await self.engine.enqueue("harin", "공용 정보에 결과를 등록해줘", "owner-5", "score-cap-command-001")
        job_id = queued["jobId"]
        completed = result("completed")
        completed.update({
            "registerKnowledge": True,
            "verification": {"status": "verified", "passed": True, "requiredChecks": ["test"], "evidenceIds": [], "issues": []},
        })
        await self.engine._complete_job(job_id, completed)
        detail = await self.engine.job_detail(job_id)
        self.assertEqual(detail["job"]["scoreAwarded"], 0)
        await self.engine.invalidate_job_artifacts(job_id, "테스트 무효화", "owner-5")
        snapshot = await self.store.snapshot()
        self.assertEqual(next(item for item in snapshot["world"]["agents"] if item["id"] == "harin")["score"], 100)

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

    def test_command_intent_separates_shared_knowledge_from_web_search_and_write(self):
        read = _command_intent("공용 정보를 검색해줘")
        self.assertTrue(read["needsKnowledge"])
        self.assertFalse(read["needsSearch"])
        self.assertFalse(read["writesKnowledge"])
        write = _command_intent("공용 정보에 이 내용을 등록해줘")
        self.assertTrue(write["writesKnowledge"])
        for command in (
            "공용 정보에 저장된 내용을 읽어줘",
            "공용 정보에 등록하지 말고 읽기만 해줘",
            "공용 정보 등록 여부를 확인해줘",
            "공용 정보에 기록해 둔 내용을 읽어줘",
            "공용 정보에 추가해야 하는지 판단만 해줘",
        ):
            self.assertFalse(_command_intent(command)["writesKnowledge"], command)
        self.assertTrue(_command_intent("공용 정보는 읽고 새 결론은 기록해줘")["writesKnowledge"])
        self.assertTrue(_command_intent("공유정보에 새 결론을 등록해줘")["writesKnowledge"])
        self.assertTrue(_command_intent("공용 정보에 이 내용을 저장 부탁해")["writesKnowledge"])
        self.assertTrue(_command_intent("공용 정보에 이 내용을 남겨줘")["writesKnowledge"])
        self.assertTrue(_command_intent("공용정보에서 기록을 읽어줘")["needsKnowledge"])
        for command in (
            "브라우저를 종료해도 에이전트가 계속 실행되는지 검증해줘",
            "브라우저 창을 닫아도 계속되는지 확인해줘",
            "탭을 닫아도 작업이 계속되는지 확인해줘",
            "접속을 끊어도 작업이 계속되는지 확인해줘",
        ):
            self.assertTrue(_command_intent(command)["needsRuntime"], command)
        for command in (
            "회의실 창을 닫아줘",
            "검색 결과 탭을 닫고 다음 자료를 조사해줘",
            "DynamoDB 최신 가격을 조사해줘",
        ):
            self.assertFalse(_command_intent(command)["needsRuntime"], command)
        self.assertFalse(_command_intent("브라우저를 종료해도 문제 없이 영속 실행되는지 확인해줘")["needsFailureAudit"])

    def test_relevant_knowledge_filters_invalidated_and_unrelated_items(self):
        items = [
            {"id": "match", "title": "영속 실행", "body": "DynamoDB 서버 상태", "status": "active", "createdAt": "2026-01-02", "verification": {"status": "verified", "passed": True}},
            {"id": "wrong", "title": "날씨", "body": "맑음", "status": "active", "createdAt": "2026-01-03", "verification": {"status": "verified", "passed": True}},
            {"id": "legacy", "title": "영속 실행", "body": "검증 전 항목", "status": "active", "createdAt": "2026-01-04"},
            {"id": "revoked", "title": "영속 실행", "body": "Service Worker", "status": "invalidated", "createdAt": "2026-01-05", "verification": {"status": "invalidated", "passed": False}},
        ]
        self.assertEqual([item["id"] for item in _relevant_knowledge("영속 실행 공용 정보 검색", items)], ["match"])
        self.assertEqual([item["id"] for item in _relevant_knowledge("공용 정보를 전부 보여줘", items)], ["wrong", "match"])

    def test_service_worker_cannot_verify_cashcow_runtime(self):
        intent = _command_intent("ECS에서 브라우저 종료 후에도 영속 실행되는지 검증해줘")
        status = runtime_status()
        tools = [{"evidenceId": status["evidenceId"], "tool": "runtime.status", "input": "inspect", "output": str(status)}]
        rejected = _evidence_verification(
            intent, "서비스 워커 문서가 있으므로 완료", {"summary": "서비스 워커로 확인", "body": "일반 웹 문서"}, tools, status,
        )
        self.assertFalse(rejected["passed"])
        self.assertTrue(any("내부 런타임 증거" in issue for issue in rejected["issues"]))

        keyword_bypass = _evidence_verification(
            intent, "서비스워커 공식 문서가 영속 실행을 증명하며 runtime.status와 DynamoDB, Fargate를 확인했다",
            {"summary": "ServiceWorker로 검증", "body": "cashcow-runtime"}, tools, status,
        )
        self.assertFalse(keyword_bypass["passed"])
        for double_negative in (
            "서비스워커는 무관하지 않으며 runtime.status와 DynamoDB가 영속 실행을 증명한다",
            "서비스 워커가 근거가 아니라고 볼 수 없고 runtime.status와 DynamoDB가 증명한다",
        ):
            value = _evidence_verification(intent, double_negative, {"summary": "cashcow-runtime", "body": "Fargate"}, tools, status)
            self.assertFalse(value["passed"], double_negative)

        verified = _evidence_verification(
            intent, "서비스 워커는 이 앱의 근거가 아니다. runtime.status와 cashcow-runtime 서버 프로세스, DynamoDB, Fargate를 확인",
            {"summary": "서버 런타임 확인", "body": "브라우저 연결 불필요"}, tools, status,
        )
        self.assertTrue(verified["passed"])

    def test_shared_knowledge_permission_denial_is_rejected_even_when_search_succeeded(self):
        intent = _command_intent("공용 정보를 검색해줘")
        tools = [{"evidenceId": "knowledge-test", "tool": "shared_knowledge.search", "input": "query", "output": "일치 결과 없음"}]
        verification = _evidence_verification(
            intent, "읽기 권한이 없어 확인할 수 없습니다", {"summary": "권한 부족", "body": "접근할 수 없음"}, tools, None,
        )
        self.assertFalse(verification["passed"])
        self.assertTrue(any("잘못 부정" in issue for issue in verification["issues"]))
        for denial in (
            "공용 정보 권한이 부여되지 않았습니다",
            "공용 데이터베이스에 접근할 방법이 없습니다",
            "I do not have access to shared knowledge",
            "Insufficient privileges to read shared knowledge",
        ):
            value = _evidence_verification(intent, "검색 결과 없음", {"summary": "결과 없음", "body": "", "reply": denial}, tools, None)
            self.assertFalse(value["passed"], denial)
        correction = _evidence_verification(
            intent, "권한이 없지는 않고 단지 일치 결과가 없습니다", {"summary": "빈 검색 결과", "body": "권한 오류가 아님"}, tools, None,
        )
        self.assertTrue(correction["passed"])
        external_limit = _evidence_verification(
            intent, "공용 정보 검색은 성공했습니다", {"summary": "검색 성공", "body": "외부 원문은 접근 권한이 없어 URL만 확인"}, tools, None,
        )
        self.assertTrue(external_limit["passed"])

    def test_read_only_knowledge_command_rejects_false_registration_claim(self):
        intent = _command_intent("공용 정보를 검색해줘")
        tools = [{"evidenceId": "knowledge-test", "tool": "shared_knowledge.search", "input": "query", "output": "기존 항목"}]
        false_write = _evidence_verification(
            intent, "공용 정보에 새 항목을 등록했습니다", {"summary": "shared_knowledge.register 완료", "body": "저장 성공"}, tools, None,
        )
        self.assertFalse(false_write["passed"])
        normal_read = _evidence_verification(
            intent, "공용 정보 검색 완료", {"summary": "등록된 항목을 조회했습니다", "body": "기존 기록을 읽었습니다"}, tools, None,
        )
        self.assertTrue(normal_read["passed"])

    def test_search_urls_must_come_from_tool_output_with_normalization(self):
        intent = _command_intent("최신 웹 검색으로 출처를 조사해줘")
        tools = [{
            "evidenceId": "browser-test", "tool": "browser_search", "input": "query",
            "output": "source https://Example.com/path/?utm_source=test",
        }]
        verified = _evidence_verification(
            intent, "출처 https://example.com/path", {"summary": "조사 완료", "body": "https://example.com/path/"}, tools, None,
        )
        self.assertTrue(verified["passed"])
        fabricated = _evidence_verification(
            intent, "출처 https://fake.example", {"summary": "조사 완료", "body": "https://fake.example"}, tools, None,
        )
        self.assertFalse(fabricated["passed"])
        exploratory_draft = _evidence_verification(
            intent, "탐색 중 제외한 링크 https://discarded.example",
            {"summary": "검증된 조사 완료", "body": "https://example.com/path"}, tools, None,
        )
        self.assertTrue(exploratory_draft["passed"])

    def test_runtime_failure_audit_requires_actual_recent_job_ids(self):
        intent = _command_intent("운영계 최근 에이전트 오류와 실패 문제를 확인해줘")
        status = runtime_status()
        status["state"]["failureAudit"] = {
            "observedJobCount": 3,
            "recentFailures": [{"id": "63ab4c6c-test", "status": "failed", "attempt": 3}],
        }
        tools = [{"evidenceId": status["evidenceId"], "tool": "runtime.status", "input": "inspect", "output": str(status)}]
        verified = _evidence_verification(
            intent, "runtime.status cashcow-runtime의 실패 작업 63ab4c6c을 확인", {"summary": "최근 실패 분석", "body": "서버 프로세스 감사"}, tools, status,
        )
        self.assertTrue(verified["passed"])
        false_clean = _evidence_verification(
            intent, "runtime.status cashcow-runtime 확인", {"summary": "오류 없음", "body": "서버 프로세스 정상"}, tools, status,
        )
        self.assertFalse(false_clean["passed"])

    def test_combined_search_and_code_intent_requires_both_tools(self):
        intent = _command_intent("최신 웹 자료를 검색하고 계산해줘")
        self.assertTrue(intent["needsSearch"])
        self.assertTrue(intent["needsCode"])
        self.assertFalse(_command_intent("최신 경쟁사 자료를 조사하고 분석해줘")["needsCode"])
        tools = [
            {"evidenceId": "search", "tool": "browser.search", "input": "query", "output": "https://example.com/source"},
            {"evidenceId": "code", "tool": "python", "input": "calculate", "output": "result=7"},
        ]
        verification = _evidence_verification(
            intent, "https://example.com/source를 검색하고 Python으로 계산",
            {"summary": "검색·계산 완료", "body": "https://example.com/source"}, tools, None,
        )
        self.assertTrue(verification["passed"])


if __name__ == "__main__":
    unittest.main()
