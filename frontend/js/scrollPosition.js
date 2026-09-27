export const EVENT_BOTTOM_THRESHOLD = 48;
export const EVENT_TOP_THRESHOLD = 60;

export function isEventStreamNearBottom({ scrollTop, clientHeight, scrollHeight }, threshold = EVENT_BOTTOM_THRESHOLD) {
  return scrollHeight - scrollTop - clientHeight <= threshold;
}

export function isEventStreamNearTop({ scrollTop }, threshold = EVENT_TOP_THRESHOLD) {
  return scrollTop <= threshold;
}
