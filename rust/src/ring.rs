//! Lock-free SPSC byte rings bridging the PipeWire RT thread and the USB (iso)
//! threads. Replaces the C mutex + drop-oldest rings in wolverine_pw.c.
//!
//! Two rings, each single-producer/single-consumer:
//!   - playback: PipeWire sink `process` (producer) -> iso OUT pump (consumer)
//!   - capture:  iso IN pump (producer) -> PipeWire source `process` (consumer)
//!
//! NOTE: rtrb only lets the *producer* stall, so on overflow we drop the NEWEST
//! bytes (unlike the C shim's drop-oldest). With rate-matched streams and OUT
//! priming the rings hover near their target fill, so overflow is a rare
//! transient — dropping newest there is fine, and it keeps a mutex out of the
//! RT process callback (the real win over the C version).

pub use rtrb::{Consumer, Producer};

/// Allocate an SPSC ring of `capacity` bytes. Returns `(producer, consumer)`.
pub fn new(capacity: usize) -> (Producer<u8>, Consumer<u8>) {
    rtrb::RingBuffer::new(capacity)
}

/// Write as much of `src` as fits; returns bytes written (rest is dropped).
pub fn write(p: &mut Producer<u8>, src: &[u8]) -> usize {
    let (pushed, _dropped) = p.push_partial_slice(src);
    pushed.len()
}

/// Read up to `dst.len()` bytes into `dst`; returns bytes read.
pub fn read(c: &mut Consumer<u8>, dst: &mut [u8]) -> usize {
    let (popped, _rest) = c.pop_partial_slice(dst);
    popped.len()
}

/// Bytes currently available to read.
pub fn avail(c: &Consumer<u8>) -> usize {
    c.slots()
}
