'use strict';
const loginForm=document.getElementById('login-form');
loginForm?.addEventListener('submit',event=>{
  event.preventDefault();
  if(!document.getElementById('username').value.trim()){document.getElementById('username').focus();return;}
  // Demonstration only. Never store or send either credential.
  loginForm.reset();
  location.href='2fa.html';
});
const password=document.getElementById('password');
document.getElementById('reveal')?.addEventListener('click',event=>{
  const show=password.type==='password';
  password.type=show?'text':'password';
  event.currentTarget.setAttribute('aria-pressed',String(show));
  event.currentTarget.setAttribute('aria-label',show?'Passwort verbergen':'Passwort anzeigen');
});
const dialog=document.getElementById('passkey-dialog');
document.querySelectorAll('[data-passkey]').forEach(button=>button.addEventListener('click',()=>dialog.showModal()));
document.getElementById('close-passkey').addEventListener('click',()=>dialog.close());
document.getElementById('confirm-passkey').addEventListener('click',()=>{dialog.close();location.href='index.html';});
const code=document.getElementById('code');
const codeArea=document.getElementById('code-area');
const error=document.getElementById('code-error');
const verifyForm=document.getElementById('verify-form');
const submitButton=verifyForm?.querySelector('button[type="submit"]');
const help=document.getElementById('code-help');
const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
const defaultHelp='Nach der sechsten Ziffer wird der Code automatisch geprüft. Einfügen ist auch möglich.';
let autoTimer, phaseTimer, busy=false;
function syncCode(animate=false){
  if(!code)return;
  document.querySelectorAll('.code-box').forEach((box,index)=>{
    const digit=code.value[index]||'';
    const changed=box.textContent!==digit;
    box.textContent=digit;
    box.classList.toggle('filled',index<code.value.length);
    box.classList.toggle('current',index===Math.min(code.selectionStart??code.value.length,5));
    if(animate&&changed&&digit&&!reducedMotion.matches){
      box.getAnimations().forEach(animation=>animation.cancel());
      box.animate([{transform:'translateY(2px) scale(.95)'},{transform:'translateY(-2px) scale(1.025)',offset:.55},{transform:'translateY(0) scale(1)'}],{duration:220,easing:'ease-out'});
    }
  });
}
function clearError(){error.textContent='';code.removeAttribute('aria-invalid');codeArea.classList.remove('invalid');help.textContent=defaultHelp;}
function scheduleSubmit(){
  clearTimeout(autoTimer);
  if(code.value.length===6&&!busy&&!dialog.open){
    autoTimer=setTimeout(()=>{if(code.value.length===6&&!busy&&!dialog.open)verifyForm.requestSubmit();},220);
  }
}
function setBusy(value){
  busy=value;
  code.readOnly=value;
  submitButton.disabled=value;
  document.getElementById('fill-code').disabled=value;
  document.querySelectorAll('[data-passkey]').forEach(button=>button.disabled=value);
  verifyForm.setAttribute('aria-busy',String(value));
}
function showError(message){
  setBusy(false);
  codeArea.classList.remove('checking','verified');
  submitButton.querySelector('span').textContent='Bestätigen & anmelden';
  help.textContent=defaultHelp;
  error.textContent=message;
  code.setAttribute('aria-invalid','true');codeArea.classList.add('invalid');
  if(!reducedMotion.matches)codeArea.animate([{transform:'translateX(0)'},{transform:'translateX(-5px)'},{transform:'translateX(4px)'},{transform:'translateX(-2px)'},{transform:'translateX(0)'}],{duration:300,easing:'ease-out'});
  code.focus();code.select();syncCode();
}
code?.addEventListener('input',event=>{
  if(event.isComposing)return;
  const caret=code.selectionStart,raw=code.value;
  code.value=raw.replace(/\D/g,'').slice(0,6);
  if(caret!==null&&raw===code.value)code.setSelectionRange(caret,caret);
  clearError();syncCode(true);scheduleSubmit();
});
code?.addEventListener('compositionend',()=>{code.value=code.value.replace(/\D/g,'').slice(0,6);clearError();syncCode(true);scheduleSubmit();});
code?.addEventListener('paste',event=>{
  if(busy){event.preventDefault();return;}
  const pasted=event.clipboardData?.getData('text');
  if(pasted===undefined)return;
  event.preventDefault();
  const digits=pasted.replace(/\D/g,'');
  if(digits.length>=6){code.value=digits.slice(0,6);code.setSelectionRange(6,6);}
  else{code.setRangeText(digits,code.selectionStart,code.selectionEnd,'end');code.value=code.value.slice(0,6);}
  clearError();syncCode(true);scheduleSubmit();
});
['focus','click','keyup','select'].forEach(name=>code?.addEventListener(name,()=>syncCode()));
document.getElementById('fill-code')?.addEventListener('click',()=>{code.value='123456';clearError();code.focus();code.setSelectionRange(6,6);syncCode(true);scheduleSubmit();});
verifyForm?.addEventListener('submit',event=>{
  event.preventDefault();clearTimeout(autoTimer);
  if(busy||dialog.open)return;
  if(code.value.length<6){showError('Bitte geben Sie alle sechs Ziffern ein.');return;}
  const submitted=code.value;
  clearError();setBusy(true);codeArea.classList.add('checking');
  help.textContent='Code wird geprüft …';
  submitButton.querySelector('span').textContent='Code wird geprüft …';
  phaseTimer=setTimeout(()=>{
    codeArea.classList.remove('checking');
    if(submitted!=='123456'){showError('Dieser Code stimmt nicht. Verwenden Sie in der Demo 123456.');return;}
    codeArea.classList.add('verified');
    help.textContent='Bestätigt. Ihre Übersicht wird geöffnet.';
    submitButton.querySelector('span').textContent='✓ Erfolgreich bestätigt';
    phaseTimer=setTimeout(()=>{code.value='';location.href='index.html';},reducedMotion.matches?120:650);
  },reducedMotion.matches?120:460);
});
document.querySelectorAll('[data-passkey]').forEach(button=>button.addEventListener('click',()=>clearTimeout(autoTimer)));
window.addEventListener('pagehide',()=>{clearTimeout(autoTimer);clearTimeout(phaseTimer);});
window.addEventListener('pageshow',event=>{
  if(event.persisted&&code){setBusy(false);code.value='';clearError();codeArea.classList.remove('checking','verified');submitButton.querySelector('span').textContent='Bestätigen & anmelden';syncCode();}
});
syncCode();
