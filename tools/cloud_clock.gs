/**
 * 기상특보 가능성 — 바깥 시계 (Google Apps Script)
 *
 * 하는 일: 5분마다 GitHub 에 "실시간 수집(live.yml)을 돌려라"는 신호 하나만 보낸다.
 *   - 자료를 받고 계산하고 배포하는 일은 전부 GitHub 이 한다. 이 스크립트는 시계 노릇만 한다.
 *   - 3시간 주기의 수집·계산은 실시간 수집이 돌 때마다 '밀렸는지' 보고 스스로 시작시킨다(teukbo/schedule.py).
 * 왜 필요한가: GitHub 의 예약 실행은 제시간을 보장하지 않고, 몇 시간씩 오지 않을 때가 있다.
 *
 * 설치(한 번만): README 의 '공개 사이트를 제때 갱신하게 하기' 를 따라 한다.
 *   1) https://script.google.com 에서 새 프로젝트를 만들고 이 파일 내용을 그대로 붙여 넣는다.
 *   2) 프로젝트 설정 → 스크립트 속성에 GH_TOKEN 을 추가한다(값: GitHub 토큰. 코드에 직접 적지 않는다).
 *   3) 트리거 → 트리거 추가 → 함수 tick · 시간 기반 · 분 단위 타이머 · 5분마다.
 *
 * 아래 세 값은 config/settings.json 의 local.trigger 와 같아야 한다(tests/test_schedule.py 가 확인한다).
 */
var REPO = 'teukbo/teukbo.github.io';
var WORKFLOW = 'live.yml';
var REF = 'main';

/** 트리거가 5분마다 부르는 함수. */
function tick() {
  var token = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  if (!token) {
    throw new Error('스크립트 속성 GH_TOKEN 이 없습니다. 프로젝트 설정 → 스크립트 속성에 GitHub 토큰을 넣어 주세요.');
  }
  var response;
  try {
    response = UrlFetchApp.fetch('https://api.github.com/repos/' + REPO + '/actions/workflows/' + WORKFLOW + '/dispatches', {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + token.trim(), Accept: 'application/vnd.github+json' },
      payload: JSON.stringify({ ref: REF }),
      muteHttpExceptions: true
    });
  } catch (e) {
    // 잠깐의 연결 문제: 알림 메일을 만들지 않고 다음 차례(5분 뒤)에 다시 보낸다.
    console.warn('GitHub 에 연결하지 못했습니다 — 다음 차례에 다시 보냅니다: ' + e);
    return;
  }
  var code = response.getResponseCode();
  if (code >= 200 && code < 300) {
    return; // 신호를 받았다
  }
  // 사람이 고쳐야 하는 문제는 오류로 남긴다(구글이 실패 알림 메일을 보낸다).
  if (code === 401) {
    throw new Error('GitHub 토큰이 맞지 않거나 기한이 지났습니다. 새 토큰을 만들어 스크립트 속성 GH_TOKEN 을 바꿔 주세요.');
  }
  if (code === 403 || code === 404) {
    throw new Error('GitHub 이 신호를 받지 않았습니다(코드 ' + code + '). 토큰을 만들 때 이 저장소를 고르고 Actions: Read and write 권한을 주었는지 확인해 주세요.');
  }
  // 그 밖(GitHub 쪽 일시 장애 등)은 기록만 하고 다음 차례에 다시 보낸다.
  console.warn('GitHub 응답 코드 ' + code + ' — 다음 차례에 다시 보냅니다: ' + response.getContentText().slice(0, 200));
}
