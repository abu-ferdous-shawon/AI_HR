(function () {
  'use strict';
  if (window.__hrAiLoader) { return; }
  window.__hrAiLoader = true;

  var BASE = (typeof window.HR_AI_BASE === 'string' ? window.HR_AI_BASE : '/ai').replace(/\/+$/, '');

  function currentUser() {
    if (window.frappe && frappe.session && frappe.session.user) { return frappe.session.user; }
    if (window.frappe && frappe.boot && frappe.boot.user && frappe.boot.user.name) { return frappe.boot.user.name; }
    return '';   // unknown: the widget checks again itself
  }

  function start() {
    if (currentUser() === 'Guest') { return; }
    if (document.getElementById('hr-ai-chat-script')) { return; }
    var s = document.createElement('script');
    s.id = 'hr-ai-chat-script';
    s.src = BASE + '/ai_chat.js';
    s.async = true;
    s.onerror = function () { console.warn('HR Assistant: could not load ' + s.src); };
    document.head.appendChild(s);
  }

  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', start); }
  else { start(); }
})();