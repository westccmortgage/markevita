(() => {
  const config = JSON.parse(document.getElementById('push-config').textContent);
  const status = document.getElementById('push-status');
  const buttons = [...document.querySelectorAll('[data-push-action]')];
  let registration, subscription;
  const message = key => { status.textContent = config.text[key]; };
  const controls = enabled => {
    buttons.forEach(button => { button.disabled = button.dataset.pushAction === 'enable' ? enabled : !enabled; });
  };
  async function api(action, value) {
    const body = new URLSearchParams({action, subscription: JSON.stringify(value), csrf_token: config.csrf});
    const response = await fetch(config.base + '/notifications/subscription', {method: 'POST', body});
    if (!response.ok) throw new Error('request');
    return response.json();
  }
  async function initialize() {
    if (!window.isSecureContext || !('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
      message('unsupported'); buttons.forEach(b => { b.disabled = true; }); return;
    }
    registration = await navigator.serviceWorker.register(config.base + '/notifications-sw.js', {scope: config.base + '/'});
    registration = await navigator.serviceWorker.ready;
    subscription = await registration.pushManager.getSubscription();
    const enabled = subscription && (await api('status', subscription.toJSON())).enabled;
    controls(enabled);
    message(Notification.permission === 'denied' ? 'blocked' : enabled ? 'enabled' : 'disabled');
  }
  buttons.forEach(button => button.addEventListener('click', async () => {
    const action = button.dataset.pushAction;
    buttons.forEach(b => { b.disabled = true; });
    try {
      if (action === 'enable') {
        // Direct user gesture: never request notification permission on page load.
        const permission = await Notification.requestPermission();
        if (permission !== 'granted') { message('blocked'); controls(false); return; }
        const response = await fetch(config.base + '/notifications/key');
        if (!response.ok) throw new Error('key');
        const {public_key: key} = await response.json();
        const bytes = Uint8Array.from(atob(key.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
        subscription = await registration.pushManager.getSubscription() || await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: bytes});
        await api('enable', subscription.toJSON());
        message('enabled'); controls(true);
      } else if (action === 'disable') {
        await api('disable', subscription.toJSON());
        await subscription.unsubscribe(); subscription = null;
        message('disabled'); controls(false);
      } else {
        await api('test', subscription.toJSON()); message('sent'); controls(true);
      }
    } catch (_) { message('error'); controls(Boolean(subscription)); }
  }));
  initialize().catch(() => { message('error'); buttons.forEach(b => { b.disabled = true; }); });
})();
