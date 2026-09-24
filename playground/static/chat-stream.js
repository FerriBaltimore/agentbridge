import { api } from './api.js';

export function createChatStream(onEvent, onEnd) {
  let source = null;

  function close() {
    if (source) source.close();
    source = null;
  }

  function open(turnId, afterSeq) {
    close();
    let stream;
    try {
      stream = api.streamTurnEvents(turnId, afterSeq);
    } catch {
      return false;
    }
    source = stream;
    stream.onmessage = (message) => {
      if (source !== stream) return;
      try {
        const event = JSON.parse(message.data);
        if (Number.isSafeInteger(event.seq) && event.seq > 0 && event.turn_id === turnId) {
          onEvent(event);
        }
      } catch { /* Polling reconciles a malformed stream frame. */ }
    };
    stream.addEventListener('end', () => {
      if (source !== stream) return;
      close();
      onEnd(turnId);
    });
    stream.addEventListener('stream.error', () => {
      if (source === stream) close();
    });
    return true;
  }

  return { open, close };
}
