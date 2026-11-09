// 슬라이드 넘김: ←/→·Space·PageUp/Down·Home/End, F 전체화면, N 발표자 노트, O 전체 보기, #번호로 바로 가기
const deck = document.getElementById('deck');
const slides = [...deck.querySelectorAll('.slide')];
const notes = document.getElementById('notes');
let cur = 0;

function fit() {
  if (document.body.classList.contains('overview')) return;
  const s = Math.min(innerWidth / 1920, innerHeight / 1080);
  deck.style.transform = `translate(${(innerWidth - 1920 * s) / 2}px, ${(innerHeight - 1080 * s) / 2}px) scale(${s})`;
}
function go(n) {
  cur = Math.max(0, Math.min(n, slides.length - 1));
  slides.forEach((s, i) => s.classList.toggle('active', i === cur));
  const aside = slides[cur].querySelector('.notes');
  notes.textContent = aside ? aside.textContent.trim().replace(/\n\s+/g, '\n') : '';
  history.replaceState(null, '', '#' + (cur + 1));
}
function overview(on) {
  document.body.classList.toggle('overview', on);
  if (!on) { fit(); go(cur); }
}

addEventListener('resize', fit);
addEventListener('hashchange', () => go((parseInt(location.hash.slice(1)) || 1) - 1));
addEventListener('keydown', e => {
  const k = e.key;
  if (['ArrowRight', 'ArrowDown', 'PageDown', ' '].includes(k)) go(cur + 1);
  else if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(k)) go(cur - 1);
  else if (k === 'Home') go(0);
  else if (k === 'End') go(slides.length - 1);
  else if (k === 'f' || k === 'F') document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
  else if (k === 'n' || k === 'N') document.body.classList.toggle('show-notes');
  else if (k === 'o' || k === 'O' || (k === 'Escape' && document.body.classList.contains('overview'))) overview(!document.body.classList.contains('overview'));
  else return;
  e.preventDefault();
});
deck.addEventListener('click', e => {
  if (e.target.closest('a')) return;   // 출처 링크는 넘김 없이 연다
  if (document.body.classList.contains('overview')) {
    const s = e.target.closest('.slide');
    if (s) { cur = slides.indexOf(s); overview(false); }
    return;
  }
  if (!getSelection().toString()) go(cur + (e.clientX > innerWidth / 2 ? 1 : -1));
});

const q = new URLSearchParams(location.search);
if (q.has('overview')) document.body.classList.add('overview');
if (q.has('clean')) document.getElementById('hint').remove();   // 캡처용: 조작 안내 숨김
fit();
go((parseInt(location.hash.slice(1)) || 1) - 1);
