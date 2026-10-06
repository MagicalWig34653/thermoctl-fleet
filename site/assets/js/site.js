'use strict';
const menuButton=document.getElementById('menu-button');
const headerNav=document.getElementById('header-nav');
menuButton?.addEventListener('click',()=>{const open=headerNav.classList.toggle('open');menuButton.setAttribute('aria-expanded',String(open));});
headerNav?.addEventListener('click',event=>{if(event.target.closest('a')){headerNav.classList.remove('open');menuButton.setAttribute('aria-expanded','false');}});
document.addEventListener('keydown',event=>{if(event.key==='Escape'){headerNav?.classList.remove('open');menuButton?.setAttribute('aria-expanded','false');}});
const reduced=matchMedia('(prefers-reduced-motion: reduce)');
if(!reduced.matches&&'IntersectionObserver' in window){document.documentElement.classList.add('js-motion');const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('visible');observer.unobserve(entry.target);}}),{threshold:.08});document.querySelectorAll('.reveal').forEach(element=>observer.observe(element));}
const progress=document.querySelector('.top-progress');
if(progress){const updateProgress=()=>{const total=document.documentElement.scrollHeight-innerHeight;progress.style.width=(total>0?scrollY/total*100:0)+'%';};window.addEventListener('scroll',updateProgress,{passive:true});window.addEventListener('resize',updateProgress);updateProgress();}
const captions={overview:'01 / Übersicht · Die wichtigsten Meldungen zuerst.',detail:'02 / Wohnungsdetails · Erreichbarkeit, Störungen und Batterien.',maintenance:'03 / Wartung · Diagnose und Sicherungen an einem Ort.'};
const previews=[...document.querySelectorAll('[data-preview]')];
function choosePreview(button){previews.forEach(tab=>{tab.setAttribute('aria-selected',String(tab===button));tab.tabIndex=tab===button?0:-1;});const panel=document.getElementById('showcase-panel'),img=panel.querySelector('img');img.src='assets/img/'+button.dataset.preview+'.webp';img.alt=captions[button.dataset.preview];panel.setAttribute('aria-labelledby',button.id);document.getElementById('preview-caption').textContent=captions[button.dataset.preview];if(!reduced.matches)img.animate([{opacity:.35,transform:'translateY(5px)'},{opacity:1,transform:'translateY(0)'}],{duration:250,easing:'ease-out'});}
previews.forEach((button,index)=>{button.addEventListener('click',()=>choosePreview(button));button.addEventListener('keydown',event=>{let next;if(['ArrowDown','ArrowRight'].includes(event.key))next=(index+1)%previews.length;if(['ArrowUp','ArrowLeft'].includes(event.key))next=(index+previews.length-1)%previews.length;if(event.key==='Home')next=0;if(event.key==='End')next=previews.length-1;if(next!==undefined){event.preventDefault();previews[next].focus();choosePreview(previews[next]);}});});
