# 아키텍처와 규칙

## 현재 MVP

```text
브라우저 픽셀 월드
  ├─ 이동·충돌·A* 경로
  ├─ 시야각·거리·벽 가림·시야 이탈
  ├─ 거리 채팅과 물리적 도구 상태
  └─ 에이전트 플랜·작업·기억·평가 UI
          │ HTTPS
Vinext / Cloudflare Worker route handlers
  ├─ ChatGPT 사용자 인증
  ├─ Groq 계획 응답 (서버 전용 키)
  ├─ 입력 검증·요청 제한·비밀값 마스킹
  └─ D1 상태·이력·평가 저장
```

월드 좌표는 `0..100`의 정규화된 공간입니다. 캐릭터 이동은 한 프레임의 최대 이동량을 제한하고, 벽·책상·소파·화이트보드를 통과하지 않습니다. 에이전트는 목표 도구까지 A* 경로로 걸어간 뒤에만 도구 상태로 전환됩니다.

시야는 기본 44칸, 정면 100도입니다. 샘플 레이 캐스팅으로 벽과 가구 뒤를 가리고, 직전 visible set과 현재 set의 차이로 `seen`과 `lost_sight`를 한 번씩 생성합니다. 소리는 발화 시점 34칸 안의 에이전트에게만 전달합니다.

## 데이터

- `agents`: 팀, 직책, 직급, 정책 버전, 성과
- `plans`: 대표 명령, 공개 가능한 단계, 상태
- `run_events`: 도구, 정제된 입력·출력, 결과
- `memories`: 경험/교훈, 근거, 신뢰도
- `whiteboards`: 제목, 글, 16×10 HEX 도트, optimistic version
- `review_cycles`, `reviews`: 서울 시간 주간 평가와 미평가 마감
- `chat_deliveries`: 발화 당시 수신 여부 이력
- `shared_knowledge`, `audit_log`: 공용 정보와 대표 평가 감사 기록

Drizzle 스키마와 생성 마이그레이션은 각각 `db/schema.ts`, `drizzle/`에 있습니다. 로컬·배포 초기화는 같은 테이블과 인덱스를 멱등 생성하고 `PRAGMA optimize`를 실행합니다.

## 에이전트 행동

```text
관찰 → 플랜 → 도구까지 이동 → 점유 → 실행 → 검증 → 교훈 저장 → 다음 작업
```

Groq는 이동 프레임마다 호출하지 않습니다. 대표 명령을 계획으로 바꾸거나 회고할 때만 호출하며 JSON 답변의 `reply`, `plan`, `lesson`만 사용합니다. 숨은 사고과정은 요청하거나 저장하지 않습니다. 실패·타임아웃·사용 불가 모델이면 결정론적 플랜으로 안전하게 폴백합니다.

## 운영 확장 경계

현재 캐릭터 위치 애니메이션은 각 브라우저에서 실행됩니다. 여러 사용자가 완전히 동일한 월드를 보거나 브라우저가 닫힌 뒤에도 에이전트가 계속 작업하려면 다음 서비스가 추가되어야 합니다.

- 서버 권위형 10Hz world loop와 WebSocket `world_seq`
- PostgreSQL snapshot + append-only event log
- 에이전트 leader advisory lock과 재시작 복구
- 격리된 스크립트 실행 워커(네트워크 차단, CPU/메모리/시간/출력 제한)
- 여러 ECS task 간 presence/pub-sub

이 확장 방향은 [AWS 운영 전환 문서](CLOUDMIDDLE_DEPLOYMENT.md)에 정리되어 있습니다.
