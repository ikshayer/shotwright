# Third-party notices

`ntsc-pipe` is a small stdin/stdout wrapper around the public `ntsc-rs` API. The
dependency is pinned in `Cargo.toml` and `Cargo.lock` to commit
`8cfc51014092a6dc1da5eb0217bb9ae6516b124e`; the active build has Rayon disabled.

The upstream `ntsc-rs` license texts are in the package root as
`LICENSE-APACHE-2.0`, `LICENSE-ISC`, and `LICENSE-MIT`. This directory contains the
license files for the exact locked active dependency versions:

| Package | Version | Declared license |
|---|---:|---|
| fearless_simd | 0.7.0 | Apache-2.0 OR MIT |
| hifijson | 0.5.1 | MIT |
| num_enum | 0.7.6 | BSD-3-Clause OR MIT OR Apache-2.0 |
| num_enum_derive | 0.7.6 | BSD-3-Clause OR MIT OR Apache-2.0 |
| proc-macro2 | 1.0.107 | MIT OR Apache-2.0 |
| quote | 1.0.47 | MIT OR Apache-2.0 |
| rustversion | 1.0.23 | MIT OR Apache-2.0 |
| syn | 2.0.119 | MIT OR Apache-2.0 |
| unicode-ident | 1.0.27 | (MIT OR Apache-2.0) AND Unicode-3.0 |

Each package directory retains the license text shipped by that crate, including
`unicode-ident`'s `LICENSE-UNICODE`. Recheck this bundle whenever `Cargo.lock` or the
enabled features change.
