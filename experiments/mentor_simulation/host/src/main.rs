//! JSONL command-line entry point for the shared Mentor service.

/// Run the same service library used by the persistent upstream mirror.
fn main() {
    mentor_simulation::run_jsonl();
}
