# 아키텍처와 규칙

## 현재 운영 구성

```text
브라우저 픽셀 월드
  ├─ 이동·충돌·A* 경로
  ├─ 시야각·거리·벽 가림·시야 이탈
  ├─ 거리 채팅과 물리적 도구 상태
  └─ 에이전트 플랜·작업·기억·평가 UI
          │ owner-only Sites route
          │ HTTPS + service token
ECS 영속 월드 런타임
  ├─ 서버 권위형 world loop와 재시작 복구
  ├─ DynamoDB 상태·작업·증거·기억 저장
  ├─ 서버 전용 Groq 호출과 비밀값 마스킹
  └─ 역할 없는 격리 브라우저 워커
       └─ 공개 HTTPS GET/HEAD 렌더링만 허용
```

월드 좌표는 `0..100`의 정규화된 공간입니다. 캐릭터 이동은 한 프레임의 최대 이동량을 제한하고, 벽·책상·소파·화이트보드를 통과하지 않습니다. 에이전트는 목표 도구까지 A* 경로로 걸어간 뒤에만 도구 상태로 전환됩니다.

시야는 기본 44칸, 정면 100도입니다. 샘플 레이 캐스팅으로 벽과 가구 뒤를 가리고, 직전 visible set과 현재 set의 차이로 `seen`과 `lost_sight`를 한 번씩 생성합니다. 소리는 발화 시점 34칸 안의 에이전트에게만 전달합니다.

## 영속 데이터와 인지 상태

- `agents`: 팀, 직책, 직급, 정책 버전, 성과
- `plans`: 대표 명령, 공개 가능한 단계, 상태
- `run_events`: 도구, 정제된 입력·출력, 결과
- `memories`: 경험/교훈, 근거, 신뢰도
- `whiteboards`: 제목, 글, 16×10 HEX 도트, optimistic version
- `review_cycles`, `reviews`: 서울 시간 주간 평가와 미평가 마감
- `chat_deliveries`: 발화 당시 수신 여부 이력
- `shared_knowledge`, `audit_log`: 공용 정보와 대표 평가 감사 기록
- `goalContract`, `checkpoints`: 성공 기준·증거 의무·다음 행동·재개 시점
- `cognition`, `relationships`: 기능적 자기 상태, 역할별 강점, 동료 신뢰 근거
- `episode`, `procedure`, `lesson`: 검증 상태와 근거 ID가 있는 경험·절차·교훈

운영의 정본은 DynamoDB의 월드 snapshot과 개별 작업·리포트 항목입니다.
로컬 런타임은 `LOCAL_STATE_PATH` 파일 저장소를 사용할 수 있습니다. 기존
Sites/D1 스키마는 UI 호환과 로컬 화면 경로를 위해 유지합니다.

## 에이전트 행동

```text
계약 → 관련 기억 회수 → 역할 배정 → 이동·점유 → 관찰·행동·재관찰
     → 성공 기준 검증 → 에피소드·절차·교훈 통합 → 다음 작업
```

Groq는 이동 프레임마다 호출하지 않습니다. 대표 명령을 계획으로 바꾸거나
도구 행동·회고를 구조화할 때만 호출합니다. 공개 판단 기록에는 관찰,
가설과 신뢰도, 대안, 선택 이유, 불확실성, 예상·실제 결과, 다음 검증만
저장하고 숨은 사고과정은 요청하거나 저장하지 않습니다. 실패·타임아웃·
사용 불가 모델이면 결정론적 플랜으로 안전하게 폴백합니다.

## 외부 읽기 보안 경계

직접 URL 관찰은 런타임 프로세스가 아니라 전용 ECS EC2 브라우저 태스크에서
실행합니다. Fargate가 비신뢰 Chromium에 필요한 custom seccomp를 지원하지
않으므로 sandbox를 끄지 않고 전용 호스트를 사용합니다. 워커에는 AWS task
role, Groq 키, 서비스 토큰, 공개 ALB 경로가 없습니다. 런타임 security
group만 내부 NLB 주소로 접근합니다. 호스트와 worker egress는 TCP/443으로
제한되며 애플리케이션은 다음을 중복 차단합니다.

- HTTP 및 비표준 포트, 사용자 정보 URL, private/link-local/metadata IP
- DNS rebinding을 막기 위한 검증 IP 고정 프록시
- GET/HEAD 외 네트워크 요청과 credential header
- WebSocket, service worker, popup, download
- 로그인·인증·전송·결제·게시·삭제 UI와 민감 입력란

호스트는 SHA 고정 Playwright seccomp와 user namespace를 적용하고,
컨테이너는 `pwuser`, read-only rootfs, `no-new-privileges`, capability 0으로
실행합니다. Docker 방화벽이 사설·loopback·link-local·metadata 목적지와
443 이외의 신규 outbound 연결을 거부합니다. 이 검증을 통과한 호스트만
ECS placement attribute를 받아 task를 실행할 수 있습니다.

외부 문서는 항상 비신뢰 관찰이며 화면 행동 뒤 URL·DOM·스크린샷 해시를
다시 기록합니다. 구체적인 배포 경계는 [AWS 운영 배포 문서](CLOUDMIDDLE_DEPLOYMENT.md)에
정리되어 있습니다.
