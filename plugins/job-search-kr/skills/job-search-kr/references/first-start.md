# 처음 시작하는 순서

새 사용자는 아래 순서로 진행한다. 각 단계에서 읽을 파일은 SKILL.md 「작업별로 읽을 파일」을 따른다.

1. **초기 설정**: 작업 폴더를 정한다. 그다음 공공 자료 조회 키를 지금 설정할지 사용자에게 묻는다. 키는 선택이지만, 묻지 않고 넘어가지 않는다.
   - 먼저 알릴 것: 키를 넣으면 기업 분석에서 공공데이터포털(기업 개요·재무, 국민연금 가입자 수, 근로복지공단 상시 인원)과 OpenDART(공시)를 조회한다. 키가 없어도 나머지 기능은 동작하고, 나중에 설정해도 된다.
   - 설정한다고 하면: 두 사이트에서 키를 발급받아 키 파일 `~/.config/job-search-kr/.env`에 `DATA_GO_KR_KEY=`, `OPENDART_KEY=` 줄로 넣게 안내한 뒤, `python3 <스킬 폴더>/scripts/company_lookup.py check`로 확인한다. 키를 대화에 붙여넣게 하지 않는다(규칙 8).
   - 나중에 한다고 하면: 기업 분석 때 조회 상태의 "키 없음"을 알리는 것으로 끝낸다([company.md](company.md) 「공공 자료 조회」 4번).
2. **자기 분석 자료**: 원본 파일을 `profile/source/`에 넣거나, 대화에 붙여넣거나, 문답이나 작업 기록 읽기로 프로필과 경험 사례를 만든다. 옮기려는 방향(이직 대전제)이 있으면 먼저 받고, 지원 대상 회사 규모, 연봉 최저선, 딜브레이커도 이때 받는다. 「자기 이해」는 [profile.md](profile.md) 「자기 이해」의 때에 받는다.
3. **목표 산업군·직무군 확인**: 목표 산업군을 고른 뒤 프로필 「목표 직무군」 표를 확인한다. 직무군을 정하지 못했으면 직군 후보를 뽑아 추천하고 사용자가 고른다([job-explore.md](job-explore.md)). 직군마다 업계·직무 용어표와 검토 기준 부록을 만든다([profile.md](profile.md) 「목표」, [job-analysis.md](job-analysis.md)).
4. **공고·기업 탐색과 평가**: 공고를 모아 평가한다. 여러 곳에 내면 지원 계획을 세우고([tracker.md](tracker.md) 「여러 곳 지원 계획」), 그 순서대로 기업 분석을 한다([collect.md](collect.md), [evaluate.md](evaluate.md), [company.md](company.md)).
5. **취업 전략 분석**: 지원마다, 또는 같은 직무·공고 유형의 묶음마다 공고 성격, 뽑고 싶은 사람, 내 위치를 정한다([strategy.md](strategy.md)).
6. **서류 작성과 검토**: 업계·직무 용어표를 확인하고 서류를 쓴 뒤 검토를 거친다(「쓰기 전 확인」, [review.md](review.md)).
7. **제출 이후**: 제출, 면접 준비, 오퍼·연봉 협상, 지원 기록, 결과 확인과 원인 분석([submit.md](submit.md), [interview.md](interview.md), [offer.md](offer.md), [tracker.md](tracker.md), [tracker-analysis.md](tracker-analysis.md)).
