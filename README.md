# 매일매일 종목찾기 — KR RS Screener

한국 주식(코스피/코스닥) 전 종목의 **기간별 수익률(1W·1M·3M·6M·12M)**과 **RS 점수(상대강도)**를 보여주는 정적 웹사이트입니다. GitHub Actions가 매일 장 마감 후 자동으로 데이터를 갱신합니다.

원본 영감: <https://joyglobal-ux.github.io/rs-screener/>

## 구조

```
.
├── index.html              # 메인 페이지 (테이블 UI)
├── data/stocks.json        # 매일 자동 생성되는 데이터 파일
├── scripts/
│   ├── update_data.py      # pykrx로 데이터 수집 + RS 계산
│   └── requirements.txt
└── .github/workflows/
    └── update.yml          # 매일 KST 19:00 자동 실행
```

## 1회만 하면 되는 배포 절차

1. **GitHub 리포지토리 생성**
   - <https://github.com/new> 에서 새 리포 만들기 (예: `rs-screener-kr`). Public 권장.

2. **이 폴더 전체를 그 리포지토리에 푸시**
   ```bash
   cd "이 폴더 경로"
   git init
   git add .
   git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<내아이디>/rs-screener-kr.git
   git push -u origin main
   ```

3. **GitHub Pages 활성화**
   - 리포지토리 → Settings → Pages
   - Source를 `Deploy from a branch`, Branch를 `main` / `/ (root)` 로 지정 후 Save
   - 잠시 후 `https://<내아이디>.github.io/rs-screener-kr/` 에서 사이트가 뜹니다.

4. **첫 데이터 한 번 수동 생성**
   - Actions 탭 → `Update RS data` 워크플로우 → `Run workflow` 클릭
   - 10~30분쯤 걸립니다 (전 종목 시세를 끌어와야 해서). 끝나면 `data/stocks.json`이 자동 커밋되고 사이트에 결과가 표시됩니다.
   - 이후로는 평일 KST 19:00에 자동으로 갱신됩니다.

## RS 점수 계산 방식

각 기간(1M, 3M, 6M, 12M) 수익률을 전 종목 대비 **백분위 순위(0~100)** 로 변환한 뒤, 다음 가중치로 평균:

| 기간 | 가중치 |
|------|--------|
| 1M   | 0.20   |
| 3M   | 0.20   |
| 6M   | 0.30   |
| 12M  | 0.30   |

점수가 높을수록 최근 시장 대비 강한 종목입니다. 가중치를 바꾸려면 `scripts/update_data.py`의 `RS_WEIGHTS`를 수정하세요.

## 로컬에서 미리보기

```bash
python -m http.server 8000
# 브라우저에서 http://localhost:8000 열기
```

`data/stocks.json`이 없으면 "데이터 로드 실패" 메시지가 뜹니다. 로컬에서 데이터를 만들고 싶으면:

```bash
pip install -r scripts/requirements.txt
python scripts/update_data.py
```

## 주의

- pykrx는 KRX 공식 데이터를 스크래핑하므로 한 번에 너무 빠르게 호출하면 차단될 수 있습니다. 스크립트에 50ms 간격 sleep이 들어 있습니다.
- 우선주, 스팩, 리츠는 종목명 패턴으로 단순 제외하고 있습니다. 더 정교한 필터가 필요하면 `fetch_universe()`를 수정하세요.
- 시가총액·섹터는 KRX 기준입니다.
