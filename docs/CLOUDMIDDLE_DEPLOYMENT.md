# AWS 운영 배포

Cashcow는 `cloudmiddle`의 서울 리전 공유 VPC, public subnet, 런타임용 ECS
cluster, ALB, CloudFront만 재사용합니다. Cashcow의 저장소, IAM, 로그와
브라우저 워커 전용 ECS EC2 cluster는 별도 Terraform state로 관리합니다.

## 운영 경로

```text
Sites(owner-only) -> CloudFront HTTPS -> ALB /cashcow/*
                                        -> persistent runtime
                                           -> DynamoDB
                                           -> Groq HTTPS
                                           -> internal NLB
                                              -> dedicated ECS EC2 browser worker
                                                -> public HTTPS GET/HEAD only
```

런타임은 서버 권위형 world loop, 작업 계약과 체크포인트, 판단·도구 증거,
에피소드·절차·교훈 기억, 역할 기반 협업 상태를 DynamoDB에 영속합니다.
브라우저가 닫히거나 ECS 태스크가 교체되어도 동일 상태에서 미완료 작업을
복구합니다.

브라우저 워커는 전용 ECS-optimized AL2023 EC2 호스트에서 실행합니다.
비신뢰 사이트용 Chromium sandbox가 요구하는 custom seccomp를 Fargate가
지원하지 않기 때문이며, sandbox 비활성화는 허용된 폴백이 아닙니다. 호스트는
버전과 SHA를 고정한 Playwright seccomp, user namespace, 컨테이너의 사설·
metadata 목적지 차단 방화벽을 모두 구성한 뒤에만 ECS에 등록됩니다.

워커는 공개 listener 없이 내부 NLB를 통해서만 발견되며 런타임 security
group만 8001 포트에 접근할 수 있습니다. task IAM role과 애플리케이션 비밀을
받지 않고, 비-root, read-only rootfs, `no-new-privileges`, capability 전체 제거
상태로 실행합니다. DNS 요청은 정확한 AmazonProvidedDNS resolver로만 제한하고,
애플리케이션 콘텐츠는 공개 HTTPS와 검증·고정한 DNS 목적지만
허용하고 외부 GET/HEAD 이외 요청, 자격증명 헤더, WebSocket, 다운로드,
service worker, 로그인·전송·결제·삭제 조작을 차단합니다. 화면과 DOM/ARIA는
비신뢰 관찰로 취급하고 각 행동 뒤 다시 관찰해 해시와 감사 증거를 남깁니다.

## 배포 게이트

`dev/predeploy.ps1`과 GitHub Quality workflow는 다음을 모두 검사합니다.

- TypeScript 타입, Vitest, ESLint, production build
- 런타임과 브라우저 워커 Python compile/unit tests
- 두 Docker 이미지의 실제 build와 동일 seccomp/권한 조건의 startup 및
  공개 HTTPS 읽기 smoke
- Terraform format/validate
- 브라우저 SSRF, DNS rebinding, 비-GET/HEAD, 자격증명 헤더, 위험한 UI
  동작 차단 테스트

운영 배포는 브라우저 워커를 먼저 안정화한 뒤 런타임을 교체합니다. 후보
이미지는 불변 commit SHA 태그로 보존하고 `latest`는 최초 bootstrap에만
사용합니다. 실행 중 task의 image digest가 SHA 태그 digest와 일치하는지
확인합니다. 런타임 공개 health의
`buildSha`도 같은 SHA여야 합니다. 브라우저 task는 별도로 `HEALTHY`, task
role 없음, secret 0개, 전용 호스트 attestation, IMDSv2 hop limit 1을
확인합니다. EC2·public IPv4·gp3·내부 NLB의 반복 비용이 생기므로 저장된
Terraform plan의 대상과 비용을 승인한 뒤에만 적용합니다.

구체적인 Terraform 적용 순서, GitHub 변수, 배포 후 읽기 전용 확인 명령은
[`infra/README.md`](../infra/README.md)에 있습니다.
