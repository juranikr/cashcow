# Cashcow HQ

포켓몬 골드처럼 위에서 내려다보는 픽셀 사무실에서 대표와 AI 에이전트가 함께 일하는 웹 MVP입니다.

## 지금 가능한 것

- 대표 캐릭터의 `WASD`/방향키 이동과 가구 충돌
- 방향·거리·벽 가림을 반영한 에이전트 시야, 발견/시야 이탈 관찰 로그
- 34칸 청취 반경과 실제로 들은 에이전트에게만 전달되는 `@이름` 명령
- 순간이동 없는 A* 격자 이동과 자율 도구 순환
- 우클릭/클릭 에이전트 상세: 현재 플랜, 단계 상태, 정제된 도구 입력·출력, 기억·교훈
- 화이트보드 제목, 배치 가능한 글 영역, 256×256 연속 도트 그림, 작성자·버전 이력
- 4대의 컴퓨터별 1인 점유와 인터넷 검색·스크립트·공용 정보 작업 지시
- 물리적 좌석 도착과 정족수를 전제로 한 회의 소집
- 주간 개인·팀 평가, 미평가 주기의 `no_review` 마감, 평가 기반 행동 정책·교훈 갱신
- 브라우저가 모두 닫혀도 실행되는 ECS 월드, DynamoDB 영속 상태, 서버 전용 Groq 호출과 비밀값 마스킹

## 로컬 실행

요구 사항은 Node.js 22.13 이상입니다.

```powershell
Copy-Item .env.example .env.local
# .env.local의 GROQ_API_KEY에 새로 발급한 키를 넣습니다.
npm install
npm run dev
```

브라우저에서 `http://localhost:3000`을 엽니다. 로컬 D1 데이터는 프로젝트의 `.wrangler` 아래에 저장됩니다.

## 검증

```powershell
npm run typecheck
npm test
npm run lint
npm run build
```

한 번에 실행하려면 다음을 사용합니다.

```powershell
./dev/predeploy.ps1
```

## 환경 변수

| 이름 | 용도 |
|---|---|
| `GROQ_API_KEY` | 서버에서만 사용하는 Groq API 키 |
| `GROQ_MODEL` | 기본값 `openai/gpt-oss-120b` |
| `SITE_ORIGIN` | Open Graph 절대 URL 기준 주소 |
| `RUNTIME_BASE_URL` | 브라우저 독립 영속 월드 HTTPS 주소 |
| `RUNTIME_SERVICE_TOKEN` | Sites와 런타임 사이의 서버 전용 인증 토큰 |

`.env.local`은 Git에서 제외됩니다. 키·원문 모델 사고과정·비정제 웹 문서는 도구 히스토리에 저장하지 않습니다.

## 문서

- [제품 및 기술 아키텍처](docs/ARCHITECTURE.md)
- [cloudmiddle 방식 AWS 운영 전환](docs/CLOUDMIDDLE_DEPLOYMENT.md)

## GitHub 자동배포

`main`의 런타임 변경은 GitHub OIDC로 AWS에 인증한 뒤 ECR과 ECS에 자동
배포됩니다. 장기 AWS 키와 Groq 키는 GitHub에 저장하지 않습니다. Sites
화면 변경은 모든 빌드 검사를 자동 수행하고, ChatGPT 로그인·D1 바인딩을
소유한 Sites 배포 경로에서 게시합니다.
