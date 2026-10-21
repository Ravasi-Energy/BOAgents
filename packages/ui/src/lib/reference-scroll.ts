import { useCallback, useEffect, useRef } from 'react';

type ScrollEnvironment = {
  Resize?: typeof ResizeObserver;
  Mutation?: typeof MutationObserver;
  frame?: typeof requestAnimationFrame;
  cancelFrame?: typeof cancelAnimationFrame;
};

/** A chosen heading follows asynchronous content growth until the user scrolls.
 * Resolve its ID again after reflow: a render can replace the heading node. */
export function createReferenceScroll(container: HTMLElement, environment: ScrollEnvironment = {}) {
  const requestFrame = environment.frame ?? requestAnimationFrame;
  const cancelFrame = environment.cancelFrame ?? cancelAnimationFrame;
  const Resize = environment.Resize ?? ResizeObserver;
  const Mutation = environment.Mutation ?? MutationObserver;
  let targetId: string | null = null;
  let pendingFrame: number | null = null;
  let disposed = false;
  const previousAnchor = container.style.overflowAnchor;
  const position = () => {
    if (disposed || !targetId) return;
    const target = container.querySelector<HTMLElement>('[id="' + targetId + '"]');
    if (!target) return;
    const top = target.getBoundingClientRect().top - container.getBoundingClientRect().top
      + container.scrollTop - container.clientTop;
    // A smooth animation retains its old destination when diagrams grow.
    // Position this scroll container directly using the current layout.
    container.scrollTo({ top: Math.max(0, top), behavior: 'instant' });
  };
  const schedule = () => {
    if (disposed || !targetId || pendingFrame !== null) return;
    pendingFrame = requestFrame(() => { pendingFrame = null; position(); });
  };
  const release = () => {
    targetId = null;
    if (pendingFrame !== null) { cancelFrame(pendingFrame); pendingFrame = null; }
    if (container.style.overflowAnchor === 'none') container.style.overflowAnchor = previousAnchor;
  };
  const onInput = (event: Event) => {
    if (event.type !== 'keydown' || ['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes((event as KeyboardEvent).key)) release();
  };
  const resize = new Resize(schedule);
  resize.observe(container);
  if (container.firstElementChild) resize.observe(container.firstElementChild);
  const mutations = new Mutation(schedule);
  mutations.observe(container, { childList: true, subtree: true });
  for (const name of ['wheel', 'touchstart', 'pointerdown', 'keydown']) container.addEventListener(name, onInput, { passive: true });
  return {
    goTo(id: string) {
      if (disposed || !/^[a-z0-9_-]+$/i.test(id) || !container.querySelector('[id="' + id + '"]')) return false;
      targetId = id;
      container.style.overflowAnchor = 'none';
      position(); schedule();
      return true;
    },
    trackedSection() { return targetId; },
    dispose() {
      release(); disposed = true;
      resize.disconnect(); mutations.disconnect();
      for (const name of ['wheel', 'touchstart', 'pointerdown', 'keydown']) container.removeEventListener(name, onInput);
    },
  };
}

export function useReferenceScroll() {
  const scrollRef = useRef<HTMLElement | null>(null);
  const controller = useRef<ReturnType<typeof createReferenceScroll> | null>(null);
  const getController = useCallback(() => {
    if (!controller.current && scrollRef.current) controller.current = createReferenceScroll(scrollRef.current);
    return controller.current;
  }, []);
  useEffect(() => {
    getController();
    return () => { controller.current?.dispose(); controller.current = null; };
  }, [getController]);
  const scrollToSection = useCallback((id: string) => getController()?.goTo(id) ?? false, [getController]);
  const getTrackedSection = useCallback(() => controller.current?.trackedSection() ?? null, []);
  return { scrollRef, scrollToSection, getTrackedSection };
}
