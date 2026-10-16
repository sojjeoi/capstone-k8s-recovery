# 네트워크 지연 감지기 개발 자료 (2026-10-17)

> **DEVELOPMENT / NOT OFFICIAL / threshold not frozen** - 사전등록 전의 개발 자료다. 임계값은 동결되지 않았고, 공식 Phase 2 결과나 Phase 1 A/B 결과와 합치지 않는다.
> 지표(`scrape_duration`, `net_probe`)는 2026-09-19 네트워크 지연 파일럿을 **본 뒤** 골랐다(파일럿도 개발 자료).
> 사전등록 초안: [docs/design/NETWORK-DETECTOR-PREREG-DRAFT-20261017.md](../../../docs/design/NETWORK-DETECTOR-PREREG-DRAFT-20261017.md)

## 내용
- `series-c3*.json`, `c1/series-c1*.json`: 정상 세션의 15초 격자 `scrape_duration_seconds` 원자료(Prometheus 보존 30일 대비 보존)
- `probe-metric/series-*.json`: 같은 세션의 kubelet readiness probe 소요 시간 tick 값
- `pilot-20260919-native-metrics.json`, `series-pilot-20260919-native.json`: 9/19 네트워크 지연 파일럿(조치 없음 arm) 시간대 지표
- `calibration.json`(개정 1), `calibration-rev2.json`(개정 2), `gate3-classification.json`, `loso-rev2.json`, `probe-metric/result.json`: 계산 결과

## 재현
- `python experiments/network_detector_calibration.py --rev2` (저장된 시계열만 사용)
- `python experiments/network_detector_gate3.py --loso`
- `python experiments/network_detector_probe_metric.py` (Prometheus 조회 필요 - 30일 보존이 지나면 불가, 이 폴더의 시계열이 원본)

## 파일 (byte-exact 저장, `.gitattributes -text`)
| 파일 | 크기(byte) | sha256 |
|---|---|---|
| `series-c1ca01.json` | 3,930 | `b223de702b3e04c3321bc8836be2ee3443e41efaf0b90ad8c8685242313d57af` |
| `series-c1ca02.json` | 4,178 | `237d33274d55bc3cc58f0e2b6b113e9a51970f2758aa9029c19ca267892f443a` |
| `series-c1ca03.json` | 4,240 | `93938ddb9763b3c3d99f1c2737bf7bb31fbf2a4c62f3d4fcbac6294a52b6b638` |
| `series-c1ca04.json` | 4,241 | `38aba9daf824906404b626ff52b5b4d07a86d6aff587a0cfa270ba69d7938249` |
| `series-c1tr01.json` | 4,083 | `a2ee22903d0659296630e1669683097d8026934932ed6e8262e031aaf7a71536` |
| `series-c1tr02.json` | 4,095 | `893231c938ee827a09f51b7cb3b064525844afa842afb484107bc1c7bf931623` |
| `series-c1tr03.json` | 3,751 | `762ca7ab67b59babe6830fd6bd72af88dfd6806e8e5e5d0778c6310c75964db7` |
| `series-c1tr04.json` | 4,245 | `6d2383fcf7b53fba6225bcf54ab7c22f25701129b87ee429682841a388496653` |
| `series-c1tr05.json` | 4,241 | `283e51849d6f953818d71c08bb6d4bf9a07efb054b1cc71dd935c92fc4163f16` |
| `series-c1tr06.json` | 4,239 | `53d80ccbbe19f51621db61d53d09a8664b4dfdf4875a5be493f1b951331f2693` |
| `calibration-rev2.json` | 4,822 | `28425bf7b4a57c87f27fa43cf08b5095521b82821e7e17a81ab9756e828f8b55` |
| `calibration.json` | 8,687 | `fdfb1dd20cebf0a01d8e378bbf451ed150ffe2c416cb3b987c87c883f305b1c2` |
| `gate3-classification.json` | 2,431 | `ed0e851f95bd0ac8d345739bf416687a834f78e3ca4daa85baca791a79a2ca7a` |
| `loso-rev2.json` | 3,110 | `83203108354d17c3693ca2ab8011e8d3abb88261e585f83e38ca9d9dd6c77c39` |
| `pilot-20260919-native-metrics.json` | 15,620 | `118358d6587f1d450456d91d2bad68d5f83b3798f8929b42b60c27cc8f723c98` |
| `result.json` | 6,119 | `a84c90199f5ac49379ea8af481bcd6e556d3ea2894978676f61c00a196fe7554` |
| `series-c1ca01.json` | 5,569 | `3acce253b5bd073fb01d377a520bad137b4f05c6fd6b93d5408cd7dee448bd86` |
| `series-c1ca02.json` | 5,775 | `4a80a847dd88c08736a6332f71873914d566ab977f0cb7a83fbcdb0f8c79deb0` |
| `series-c1ca03.json` | 5,732 | `c98bf181c7d7a7c9d4f8f53927de40a079043f6023759be40d5cd207b2981899` |
| `series-c1ca04.json` | 5,856 | `f31e2a9418968ba47d53729d4303ceb2c705dc1cf70e01590d3d34389e910589` |
| `series-c1tr01.json` | 5,636 | `8142e3c746411314d4aeb463cd793159b4704ba20b5213e005fb378390f9c575` |
| `series-c1tr02.json` | 5,621 | `c39350137f66853f5922657870b78d9b8e30d28e58efb8e03719d5b6d8d6e71f` |
| `series-c1tr03.json` | 5,172 | `ae012fa2cf967c2a44ca21f22fef152a0bde7046d34e7dcca5398288824b3c36` |
| `series-c1tr04.json` | 5,863 | `ea8a799ab7f5a69d733b6346b696d261fe68a042cbc4edb0e0fc50726207420b` |
| `series-c1tr05.json` | 5,836 | `324a1cea7b25ab1a87df72fc0b498a48e63913ca63ed7258e4e1a26500b55eb2` |
| `series-c1tr06.json` | 5,838 | `456b4aec27052d13faa52578e6c7cb853ad7da85df2ad68f3b80188940290854` |
| `series-c3n01.json` | 12,393 | `9915de9e8603c661ea62f109debf022c1848b08ec0092c382ee8f991b0d36c2b` |
| `series-c3n02.json` | 12,410 | `94195de6c42f612dbebccb31d1bbf648a735d5ba218aeaa864dece7fd5f3e317` |
| `series-c3n03.json` | 12,416 | `6c1dede6b3e035bb484a95101718522af3b445cade249f578f417a101784de7b` |
| `series-c3n04.json` | 12,384 | `972e4b011826d446a5356b69f1adf9e6e87e1bbb9c6f7209da6b9ac0ca8ad0b0` |
| `series-c3n06.json` | 12,452 | `f9c6dd1698dfbd6e56ce8c91e904200659fdbe52a915bf2aa0d5864c626442a5` |
| `series-c3n07.json` | 12,440 | `a66ac8a188dd1d0490b1dd37938f4795a309882d385ab586a955b16c22dcc485` |
| `series-c3n08.json` | 12,398 | `142f2b2d9ad895875ac01c59289e78e980a107a979f5747950b2843d9fe66486` |
| `series-c3n09.json` | 12,450 | `8b6be0b5d0968348b06a9f9c4151b51e202946fdcc09dcb1f3ecc3e7e6448a33` |
| `series-c3n10.json` | 12,411 | `c486dfaa77ee68355bade178cc07174fa1e5f0a8fe87ae1f986f644c60179554` |
| `series-c3r01.json` | 12,429 | `157460451dd23b929c9665c1f90c7cffad96d5b192c4a6e703938b7be61614b9` |
| `series-pilot-20260919-native.json` | 1,825 | `2347b61bc21f21795189691f96216ee86852870aec48a9d1bd2faef41e9411f1` |
| `series-c3n01.json` | 8,897 | `265dff7ae411ac2765158f8b8a37f354b6027e132d86207c3107d711c7dfeb2e` |
| `series-c3n02.json` | 8,916 | `041eb7bc38c7feade59d68084766c0bac8928c3a0135a5a3a6611102e4d2a46c` |
| `series-c3n03.json` | 8,905 | `88bae0f9762530abfbd55a6166b797d9c4855952f640378432007ffb1f68750c` |
| `series-c3n04.json` | 8,617 | `af61088164b3c63603433ccab062771d40f51ddd8e4ac5785982462a853a049c` |
| `series-c3n05.json` | 4,050 | `bdd72fc16f7517c27162ff6afd4762ea2872de4854fd228e40178a5f4a338e67` |
| `series-c3n06.json` | 8,941 | `d60e826e83ca15f3c2f11aa46fb4270966132b698fa1bd21b7128dc28818140a` |
| `series-c3n07.json` | 8,904 | `591d4f446ee63523e84cebd607807bb429b8d58689a913719dcaadbc373ff7a0` |
| `series-c3n08.json` | 8,908 | `a0f4a5bb9ebbd42e97ac1de5636627793c26d70bae48b3b34dfacbbd84bdf88c` |
| `series-c3n09.json` | 8,942 | `70e886a5f40748f407d1c1a4247b21122db4e0ea74d557429a28749267cbc2f9` |
| `series-c3n10.json` | 8,902 | `8fa8fb16d5b4264c6b7976f8014859182750527f30656a6169f177cf50618300` |
| `series-c3r01.json` | 8,901 | `529bb07d3281cc408328f27ef908b5f53039aab774e27e512542133e24ed9485` |
| `series-pilot-20260919-native.json` | 1,370 | `87d4114fb52cb4ea9b316a04914bdcf9999f35e466ab28661ddbdb2bbbc67d29` |
