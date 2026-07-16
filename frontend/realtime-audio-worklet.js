/*
 * Realtime microphone frame collector.
 *
 * AudioWorklet runs on the browser's audio rendering thread instead of the UI/main thread. Keeping capture
 * here prevents transcript rendering, scrolling, or dialog work from delaying microphone callbacks and
 * dropping syllables. The main thread still owns WebSocket transport and product state.
 */

const FRAME_SAMPLES = 1024;

class RealtimePcmCollector extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Float32Array(FRAME_SAMPLES);
    this.offset = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel?.length) return true;

    let sourceOffset = 0;
    while (sourceOffset < channel.length) {
      const copyLength = Math.min(channel.length - sourceOffset, FRAME_SAMPLES - this.offset);
      this.frame.set(channel.subarray(sourceOffset, sourceOffset + copyLength), this.offset);
      this.offset += copyLength;
      sourceOffset += copyLength;

      if (this.offset === FRAME_SAMPLES) {
        // Transfer the underlying buffer instead of cloning it. A fresh frame is allocated immediately, so the
        // audio thread never reuses memory that has already moved to the main thread.
        const completedFrame = this.frame;
        this.port.postMessage(completedFrame, [completedFrame.buffer]);
        this.frame = new Float32Array(FRAME_SAMPLES);
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor("realtime-pcm-collector", RealtimePcmCollector);
