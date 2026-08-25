import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cognition import (
    begin_commitment,
    complete_reflection,
    consolidate_procedure,
    make_goal_contract,
    relevant_memories,
    staff_brief,
    update_checkpoint,
    update_relationship,
)
from engine import MAX_ATTEMPTS, WorldEngine, _command_intent
from storage import FileStore, ensure_data_shape, initial_data


NOW = "2026-08-25T00:00:00Z"


def verified_result(*, outcome="completed", collaboration=None, passed=True):
    verification = {
        "status": "verified" if passed else "rejected",
        "passed": passed,
        "requiredChecks": ["report.content", "job.events"],
        "evidenceIds": ["evidence-1"],
        "issues": [] if passed else ["필수 근거가 없습니다."],
    }
    return {
        "reply": "검증 가능한 결과를 보고합니다.",
        "publicDecision": {
            "observations": ["실제 도구 결과를 확인함"],
            "objective": "완료 기준을 증거로 검증",
            "hypotheses": [{
                "statement": "관찰 결과가 완료 기준을 충족한다.",
                "confidence": 0.9,
                "evidenceRefs": ["evidence-1"],
            }],
            "chosenAction": "검증된 결과를 보고",
            "rationale": "실제 결과와 성공 기준이 대응함",
            "alternatives": [{
                "action": "추가 실행",
                "expectedBenefit": "근거 보강",
                "rejectedBecause": "현재 근거로 기준이 충족됨",
            }],
            "evidenceRefs": ["evidence-1"],
            "uncertainties": [],
            "expectedResult": "필수 기준 통과",
            "actualResult": "필수 기준 통과",
            "nextChecks": ["완료 계약 상태 확인"],
            "blockers": [],
            "confidence": 0.9,
        },
        "toolRuns": [{
            "evidenceId": "evidence-1",
            "tool": "test.read",
            "input": "inspect",
            "output": "observed result",
        }],
        "collaboration": collaboration or [],
        "registerKnowledge": False,
        "verification": verification,
        "report": {
            "title": "검증 리포트",
            "outcome": outcome,
            "summary": "관찰 결과가 완료 기준을 충족했습니다.",
            "body": "실제 도구 결과와 완료 기준을 대조했습니다.",
            "limitations": [] if passed else list(verification["issues"]),
            "lessons": ["도구 증거를 성공 기준과 직접 연결한다."],
        },
    }


class CognitionModelTests(unittest.TestCase):
    def test_shape_migration_preserves_valid_memory_and_adds_peer_relationships(self):
        data = initial_data()
        agent = data["world"]["agents"][0]
        agent.pop("cognition")
        agent.pop("relationships")
        agent["memories"] = [
            "corrupt",
            {"id": "legacy", "summary": "검증된 레거시 교훈", "kind": "unknown"},
        ]

        self.assertTrue(ensure_data_shape(data))
        self.assertEqual([item["id"] for item in agent["memories"]], ["legacy"])
        self.assertEqual(agent["memories"][0]["kind"], "lesson")
        self.assertEqual(set(agent["relationships"]), {"doyun", "harin", "jun"})
        self.assertEqual(agent["cognition"]["model"], "functional-simulation-v1")

    def test_goal_contract_marks_direct_browser_work_as_external_read_only(self):
        command = "https://example.com 페이지의 DOM을 살펴봐"
        intent = _command_intent(command)
        contract = make_goal_contract(command, intent, "computer", NOW)

        self.assertTrue(intent["needsDirectBrowser"])
        self.assertEqual(contract["riskTier"], "external-read")
        self.assertTrue(any(item["verifier"] == "browser.dom.observe" for item in contract["successCriteria"]))
        self.assertTrue(any("상태 변경" in item and "실행하지 않는다" in item for item in contract["proofObligations"]))
        self.assertTrue(_command_intent("https://example.com 내용을 확인해줘")["needsDirectBrowser"])

    def test_staff_brief_exposes_functional_duty_not_claimed_consciousness(self):
        agent = initial_data()["world"]["agents"][0]
        brief = staff_brief(agent)
        self.assertIn("기능적 상태", brief["disclosure"])
        self.assertGreaterEqual(brief["traits"]["conscientiousness"], 0.8)
        self.assertIn("증거가 부족하면 완료하지 않는다", brief["duty"])
        self.assertEqual(brief["role"]["id"], agent["id"])

    def test_memory_recall_uses_only_relevant_verified_experience_procedure_or_lesson(self):
        agent = {
            "memories": [
                {"id": "episode", "kind": "episode", "summary": "브라우저 DOM 해시 관찰 경험", "verificationStatus": "verified", "utility": 0.7, "at": "1"},
                {"id": "procedure", "kind": "procedure", "summary": "브라우저 DOM을 먼저 읽는 절차", "verificationStatus": "asserted", "utility": 0.6, "at": "2"},
                {"id": "unverified", "kind": "lesson", "summary": "브라우저 DOM을 조작한다", "utility": 1.0, "at": "3"},
                {"id": "invalid", "kind": "lesson", "summary": "브라우저 DOM 관찰", "verificationStatus": "invalidated", "utility": 1.0, "at": "4"},
                {"id": "unrelated", "kind": "lesson", "summary": "화이트보드 색상 선택", "verificationStatus": "verified", "utility": 1.0, "at": "5"},
            ]
        }

        recalled = relevant_memories(agent, "브라우저 DOM을 관찰해줘")

        self.assertEqual({item["id"] for item in recalled}, {"episode", "procedure"})
        recalled[0]["summary"] = "mutated"
        self.assertNotEqual(agent["memories"][0]["summary"], "mutated")

    def test_commitment_checkpoint_reflection_and_relationship_are_calibrated(self):
        agent = initial_data()["world"]["agents"][0]
        contract = make_goal_contract("상태를 검증해줘", _command_intent("상태를 검증해줘"), "computer", NOW)
        begin_commitment(agent, contract, [], NOW)
        update_checkpoint(agent, "첫 관찰 성공", "최종 검증", 0.8, "2026-08-25T00:01:00Z")
        episode = complete_reflection(
            agent,
            {"id": "job-1", "tool": "computer", "plan": []},
            {"id": "report-1", "outcome": "completed", "summary": "검증 완료"},
            {"status": "verified", "passed": True, "evidenceIds": ["evidence-1"], "issues": []},
            "2026-08-25T00:02:00Z",
        )
        procedure = consolidate_procedure(
            agent,
            {"id": "job-1", "tool": "computer", "plan": []},
            {"status": "verified", "passed": True, "evidenceIds": ["evidence-1"], "issues": []},
            "2026-08-25T00:02:00Z",
        )

        self.assertEqual(episode["kind"], "episode")
        self.assertEqual(procedure["kind"], "procedure")
        self.assertEqual(episode["verificationStatus"], "verified")
        self.assertEqual(agent["cognition"]["selfModel"]["jobsCompleted"], 1)
        self.assertTrue(agent["cognition"]["selfModel"]["procedures"])
        self.assertEqual(agent["cognition"]["commitment"]["status"], "completed")
        self.assertEqual(agent["cognition"]["state"]["commitmentStrength"], 0.0)

        update_relationship(agent, "doyun", True, "2026-08-25T00:03:00Z")
        relation = agent["relationships"]["doyun"]
        self.assertEqual(relation["commitmentsMet"], 1)
        self.assertEqual(relation["sharedSuccesses"], 1)
        self.assertGreater(relation["trust"], 0.62)


class CognitionEngineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "world.json")
        self.store = FileStore(self.path)
        await self.store.initialize()
        self.engine = WorldEngine(self.store)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_contract_and_cognition_survive_reopen_and_verified_completion(self):
        queued = await self.engine.enqueue("harin", "관찰 결과를 검증해줘", "owner", "contract-1")
        job_id = queued["jobId"]

        reopened_store = FileStore(self.path)
        await reopened_store.initialize()
        reopened_engine = WorldEngine(reopened_store)
        before = await reopened_store.snapshot()
        before_job = before["jobs"][job_id]
        before_agent = next(item for item in before["world"]["agents"] if item["id"] == "harin")
        self.assertEqual(before_job["goalContract"]["status"], "committed")
        self.assertEqual(before_agent["cognition"]["commitment"]["objective"], before_job["goalContract"]["objective"])

        await reopened_engine._complete_job(job_id, verified_result())
        after = await reopened_store.snapshot()
        job = after["jobs"][job_id]
        agent = next(item for item in after["world"]["agents"] if item["id"] == "harin")

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["goalContract"]["status"], "completed")
        self.assertTrue(all(item["status"] == "passed" for item in job["goalContract"]["successCriteria"] if item["required"]))
        self.assertTrue(all(item["evidenceIds"] for item in job["goalContract"]["successCriteria"] if item["required"]))
        self.assertEqual(agent["cognition"]["selfModel"]["jobsCompleted"], 1)
        self.assertEqual(agent["cognition"]["commitment"]["status"], "completed")
        self.assertTrue({"episode", "procedure", "lesson"}.issubset({item["kind"] for item in agent["memories"]}))
        decisions = [item["summary"] for item in job["events"] if item["type"] == "decision"]
        self.assertGreaterEqual(len(decisions), 2)
        for decision in decisions:
            self.assertTrue({
                "observations", "objective", "hypotheses", "chosenAction", "rationale",
                "alternatives", "evidenceRefs", "uncertainties", "expectedResult",
                "actualResult", "nextChecks", "blockers", "confidence",
            }.issubset(decision))

    async def test_queued_contract_does_not_replace_the_active_commitment(self):
        first = await self.engine.enqueue("harin", "첫 번째 상태를 검증해줘", "owner", "queue-contract-1")
        second = await self.engine.enqueue("harin", "두 번째 상태를 검증해줘", "owner", "queue-contract-2")
        before = await self.store.snapshot()
        agent = next(item for item in before["world"]["agents"] if item["id"] == "harin")

        self.assertEqual(agent["activeJobId"], first["jobId"])
        self.assertEqual(before["jobs"][second["jobId"]]["status"], "queued")
        self.assertEqual(agent["cognition"]["commitment"]["objective"], before["jobs"][first["jobId"]]["command"])

        await self.engine._complete_job(first["jobId"], verified_result())
        after = await self.store.snapshot()
        agent = next(item for item in after["world"]["agents"] if item["id"] == "harin")
        self.assertEqual(agent["activeJobId"], second["jobId"])
        self.assertEqual(agent["cognition"]["commitment"]["objective"], after["jobs"][second["jobId"]]["command"])

    async def test_replan_records_changed_strategy_and_activates_correct_phase(self):
        queued = await self.engine.enqueue("harin", "관찰 결과를 검증해줘", "owner", "retry-1")
        job_id = queued["jobId"]
        await self.store.mutate(lambda data: data["jobs"][job_id].update({"attempt": 1, "phase": "executing"}))

        await self.engine._complete_job(job_id, verified_result(outcome="partial", passed=False))
        job = (await self.store.snapshot())["jobs"][job_id]
        phases = {item["phase"]: item["status"] for item in job["plan"]}

        self.assertEqual(job["status"], "active")
        self.assertEqual(job["planRevision"], 2)
        self.assertEqual(len(job["goalContract"]["strategyHistory"]), 1)
        self.assertEqual(phases["move"], "done")
        self.assertEqual(phases["act"], "active")
        self.assertEqual(phases["verify"], "blocked")

    async def test_completed_claim_cannot_bypass_failed_or_missing_verification(self):
        for suffix, result in (
            ("rejected", verified_result(outcome="completed", passed=False)),
            ("missing", {key: value for key, value in verified_result().items() if key != "verification"}),
        ):
            with self.subTest(suffix=suffix):
                queued = await self.engine.enqueue("harin", "관찰 결과를 검증해줘", "owner", f"fail-closed-{suffix}")
                job_id = queued["jobId"]
                await self.store.mutate(lambda data, target=job_id: data["jobs"][target].update({"attempt": MAX_ATTEMPTS, "phase": "executing"}))
                await self.engine._complete_job(job_id, result)
                snapshot = await self.store.snapshot()
                job = snapshot["jobs"][job_id]
                report = next(item for item in snapshot["reports"] if item["planId"] == job_id)
                self.assertEqual(job["status"], "failed")
                self.assertNotEqual(report["outcome"], "completed")

    async def test_recorded_role_collaboration_updates_relationship_and_persists(self):
        queued = await self.engine.enqueue("harin", "회의실에서 역할별로 독립 검토해줘", "owner", "collab-1")
        job_id = queued["jobId"]
        collaboration = [{
            "toAgentId": "doyun",
            "purpose": "리서처 근거 검토",
            "message": "출처 근거를 독립 검토해 주세요.",
            "response": "근거와 결론이 대응합니다.",
        }]

        active = await self.store.snapshot()
        for participant in active["world"]["agents"]:
            commitment = participant["cognition"]["commitment"]
            self.assertIsInstance(commitment, dict, participant["id"])
            self.assertEqual(
                commitment["objective"],
                active["jobs"][job_id]["command"],
                participant["id"],
            )

        await self.engine._complete_job(job_id, verified_result(collaboration=collaboration))
        reopened = FileStore(self.path)
        await reopened.initialize()
        snapshot = await reopened.snapshot()
        harin = next(item for item in snapshot["world"]["agents"] if item["id"] == "harin")
        relation = harin["relationships"]["doyun"]

        self.assertEqual(relation["commitmentsMet"], 1)
        self.assertEqual(relation["sharedSuccesses"], 1)
        self.assertGreater(relation["familiarity"], 0.18)
        event = next(item for item in snapshot["jobs"][job_id]["events"] if item["type"] == "agent_message")
        self.assertEqual(event["recipientAgentId"], "doyun")

    async def test_url_command_observes_isolated_browser_without_model_key(self):
        queued = await self.engine.enqueue("doyun", "https://example.com 페이지를 살펴봐", "owner", "browser-observe-1")
        snapshot = await self.store.snapshot()
        job = snapshot["jobs"][queued["jobId"]]
        agent = next(item for item in snapshot["world"]["agents"] if item["id"] == "doyun")
        observation = {
            "url": "https://example.com/",
            "title": "Example Domain",
            "httpStatus": 200,
            "renderedText": "This domain is for use in illustrative examples.",
            "domHash": "a" * 64,
            "screenshotSha256": "b" * 64,
            "untrustedObservation": True,
        }
        tool_runs = [{
            "evidenceId": "dom-observation-test",
            "tool": "browser.dom.observe",
            "input": '{"url": "https://example.com"}',
            "output": (
                '{"url": "https://example.com/", "title": "Example Domain", '
                '"renderedText": "This domain is for use in illustrative examples.", '
                '"domHash": "' + ("a" * 64) + '", "screenshotSha256": "' + ("b" * 64) + '"}'
            ),
        }]

        async def fake_browser(_job, _intent, _agent=None):
            return tool_runs, [observation]

        with patch.dict("os.environ", {"GROQ_API_KEY": "", "BROWSER_WORKER_URL": "http://127.0.0.1:8001"}, clear=False):
            with patch.object(self.engine, "_run_direct_browser", fake_browser):
                result = await self.engine._groq_work(job, agent, [agent], [], [], self.engine._runtime_status(snapshot))

        self.assertTrue(any(item["tool"] == "browser.dom.observe" for item in result["toolRuns"]))
        self.assertIn("https://example.com", result["report"]["summary"])
        self.assertIn("illustrative examples", result["report"]["body"])
        self.assertTrue(result["verification"]["passed"])


if __name__ == "__main__":
    unittest.main()
