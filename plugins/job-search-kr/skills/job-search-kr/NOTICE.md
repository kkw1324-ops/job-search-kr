# 참고한 저장소

이 스킬은 아래 공개 저장소의 방법론(절차와 점검 항목의 착안)을 참고해 지침과 코드를 새로 썼다. 원문 문장과 코드는 가져오지 않았다. 배포 전에 각 저장소와 문장·코드 겹침을 검사했고, 겹친 것은 흔한 상투어 목록과 표준 라이브러리 관용구 수준이었다. 라이선스 의무 때문이 아니라 착안의 출처를 밝히려고 적는다.

| 저장소 | 저작권자 | 참고한 것 |
|---|---|---|
| https://github.com/career-ops-hq/career-ops | Copyright (c) 2026 Santiago Fernández de Valderrama | 공고 평가 절차(열림 확인, 요건과 증거 표, 공고 신뢰성, 위험 요약), 한국 근로 조건 표와 협상 문구, 면접 준비·복기·회사 쪽 신호, 오퍼 검토 열 가지, 후속 연락 주기, 탈락 패턴 분석 |
| https://github.com/MadsLorentzen/ai-job-search | Copyright (c) 2026 Mads Lorentzen | 프로필 항목과 일하는 방식 질문, 관문 검사, 가중치 점수, 바꿔 써도 되는 범위, 지원서 문항과 글자 수 처리, 검토 에이전트 단계 |
| https://github.com/thesun4sky/jobstack | Copyright (c) 2026 jobstack contributors | 기업 분석 항목과 키워드 대조표, 국문 이력서 항목과 흔한 실수, 경력기술서 틀, 세 문서 역할 구분, 자소서 문항 유형과 결론·이유·요청 구조, 모의면접 형식과 평가 항목, NCS 대응 |
| https://github.com/Paramchoudhary/ResumeSkills | Copyright (c) 2026 Resume Skills | 영문 문장 공식(XYZ·CAR), 키워드 배치, 커버레터 구조, LinkedIn 분량 |
| https://github.com/varunr89/resume-tailoring-skill | Copyright (c) 2025 Varun Ramesh | 숨은 경험을 끌어내는 질문 흐름 |
| https://github.com/mohitagw15856/pm-claude-skills | Copyright (c) 2026 Mohit Aggarwal | 기술란 항목마다 쓴 곳 확인(career-inventory), 고칠 문장 상위 3개 표시(recruiter-scan-simulator), 필수 미충족 때 추천 경로 묻기(jd-gap-score), 의심 문장 점검(cv-honesty-check) |
| https://github.com/curiositech/port-daddy | Functional Source License 1.1 (MIT Future License), Copyright (c) 2026 Erich Owens / Curiositech LLC. 개념만 참고, 코드·원문 없음 | 판단 지점마다 묻는 질문(Critical Decision Method 개념). profile.md 「판단 장면 인터뷰」 |
| https://github.com/yanliudesign/offer-toolkit-skill | MIT | 반사실 질문으로 사례 고르기, 「우리」를 본인 몫으로 되돌리기, 숫자를 기록으로 확인하는 질문 |
| https://github.com/squerne/open-career-skills | MIT | 작업 기록을 묶음으로 찾고 「기록이 보여 주는 것」과 「본인만 아는 것」을 나누기(evidence-miner) |
| https://github.com/m2ai-portfolio/m2ai-skills-pack | MIT | 사례 문장 깊이 점검 3단계(promotion-packet-auditor) |
| https://github.com/soom-kang/write-me | MIT | 고칠 때 지킬 뜻(가능·의무·부정·인과), 교정 결과 세 갈래 |
| https://github.com/nathankim0/humanize-korean | MIT | 확신이 없는 교정을 오류로 확정하지 않기(반례), 수치 보존 검사 |
| https://github.com/epoko77-ai/im-not-ai | MIT | 바뀐 비율 경고·중단 문턱, 서법 표현 소실 검사. revision_check.py는 새로 짰다 |
| https://github.com/LabRAI/Rulers | MIT | 판정마다 한 단계 위·아래가 아닌 이유를 쓰게 하기 |
| https://github.com/NomaDamas/k-skill | MIT | 기업 분석의 사실·회사 설명·해석 구분, 수치 표기 요소, 「확인 불가」 중단 기준(company-analysis 지침 문서만 참고, 실행 구조는 쓰지 않음) |
| https://github.com/anthropics/financial-services | Apache-2.0 | 출처가 충돌할 때 채택 순서, 추정치 표시, 업종 핵심 지표와 가치사슬 |
| https://github.com/anthropics/claude-cookbooks | MIT | 수집 결과의 출처 의심 신호 표시 |

## 가져오지 않은 것

위 저장소를 점검하면서 찾은 위험한 방식은 쓰지 않았다.
- 스킬이 셸 명령을 제한 없이 허용하는 설정 (남은 위험: 수집 에이전트(job-collector)는 Bash 전체를 쓸 수 있고, 따옴표 인자·허용 명령 제한은 지시문에만 있다)
- 공고에서 가져온 회사명·직무명·주소를 확인 없이 셸 명령 문자열에 넣는 방식 (수집 에이전트는 위임 프롬프트에 있거나 http(s) 형식을 확인한 주소만 따옴표 인자로 넘기고, 공고 안의 다른 글은 셸 명령에 넣지 않는다)
- 권한 확인을 끄고 에이전트를 여럿 띄우는 일괄 처리
- 사용자 확인 없이 브라우저로 지원서를 채우고 제출하는 기능 (본인 구직 사이트 프로필은 사용자 요청으로 채운다. 지원서 제출은 사용자가 맡기고 제출 직전 요약을 확인한 뒤에만 한다)
- 봇 차단 우회, 로그인이 필요한 사이트 수집
- 메일·웹 글을 스킬이나 지침 파일에 기록하는 방식
- 자동 업데이트, 예약 실행
- 수치를 추정해서 채우게 하는 예시 (AI는 근거와 계산식이 붙은 추산값을 추천하고 사용자가 확인한 값만 쓴다. 확인 없이 채우는 방식은 쓰지 않는다)
- 다른 사람의 프로필 수집
