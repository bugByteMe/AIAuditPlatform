export const EVENT_BOTTOM_THRESHOLD = 48;

export function isEventStreamNearBottom({ scrollTop, clientHeight, scrollHeight }, threshold = EVENT_BOTTOM_THRESHOLD) {
  return scrollHeight - scrollTop - clientHeight <= threshold;
}
