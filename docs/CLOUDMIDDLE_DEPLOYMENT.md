# cloudmiddle 방식 AWS 운영 전환

`cloudmiddle`은 Vite 정적 프런트엔드와 FastAPI/PostgreSQL을 한 Docker 이미지로 만들고, GitHub OIDC → ECR → ECS Fargate로 배포합니다. Terraform이 VPC, ALB, ECS, RDS, ECR, Secrets Manager, CloudFront를 관리합니다.

Cashcow HQ의 현재 실행 대상은 Cloudflare Worker + D1이므로 같은 Dockerfile을 그대로 복사하면 정상 운영되지 않습니다. AWS 운영판은 UI를 재사용하고 아래의 authoritative backend를 먼저 추가한 뒤 `cloudmiddle` 파이프라인을 적용해야 합니다.

## 목표 구성

```text
CloudFront → ALB (WebSocket 포함) → ECS Fargate
                                      ├─ FastAPI world/API
                                      └─ 정적 React UI
                                             │
                                      private RDS PostgreSQL

EventBridge → Step Functions → one-off Fargate agent workers
Secrets Manager → GROQ_API_KEY / DB credentials
GitHub OIDC → ECR image push → immutable task revision → ECS deploy
```

## cloudmiddle에서 그대로 가져갈 관례

- 서울 리전 `ap-northeast-2`
- 멀티스테이지 프런트엔드 + Python 이미지
- 로컬 PostgreSQL Docker Compose와 healthcheck
- SHA 이미지 태그, ECR, ECS 안정화 대기
- Terraform S3 remote state + DynamoDB lock
- GitHub Actions OIDC (장기 AWS access key 금지)
- Groq 키와 DB 비밀번호의 Secrets Manager 주입

## Cashcow에서 보강할 점

- RDS는 private subnet에 두고 PostgreSQL ingress를 ECS security group에만 허용
- ALB idle timeout보다 짧은 15~25초 WebSocket heartbeat
- API/WS 경로의 CloudFront 캐시 비활성화
- ECS desired count 1에서 시작하고 world leader는 PostgreSQL advisory lock 사용
- 스크립트 실행은 API 컨테이너와 분리된 비특권 one-off task로 실행
- `latest` 강제 재시작 대신 SHA가 고정된 task definition revision으로 롤백 가능하게 구성
- 앱 workflow 전에 타입 검사, 단위 테스트, 빌드, DB migration 검사를 강제

## 대표가 제공해야 하는 값

1. 새 GitHub 저장소 또는 기존 저장소 이름과 기본 브랜치
2. AWS 계정/리전, 운영 도메인, 예상 동시 사용자·에이전트 수
3. GitHub OIDC role ARN과 Terraform remote-state bucket/table 이름
4. 폐기·재발급한 Groq 키(Secrets Manager에만 등록)
5. 주간 평가 알림을 인앱 외에 이메일/Slack으로도 보낼지 여부

AWS 리소스 생성은 비용과 외부 변경이 발생하므로 이 값들이 확정된 뒤 별도 Terraform state로 적용합니다. `cloudmiddle`의 기존 state나 공개 RDS 규칙을 공유하지 않습니다.
