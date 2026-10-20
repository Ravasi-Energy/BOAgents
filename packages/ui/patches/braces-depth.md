# Downstream braces depth control

GHSA-vfj7-8cjw-p6xm, MIT braces3.0.3; no upstream fixed version claimed.

`postinstall` applies only exact registry preimages (SHA256); `predev`, `prebuild`, `prestart`, `pretest` verify all postimages. An ignored/partial postinstall fails closed. No new dependency and no suppressed audit. Production installs omitting all declared dev-only braces consumers may report ABSENT_DEV_ONLY. A change in version/source/license fails before writes and requires review.

All four direct `lib` entrypoints are covered. Parser refuses AST depth before recursive helpers; iterative prewalk fences direct ASTs, cycles, parent-chain depth/cycles, node count, and invalid options before mutation. Depth ceiling128 and nodes65536, callers may tighten only with `maxDepth`/`maxNodes`; no Infinity/zero/raised ceiling bypass. This intentionally rejects extreme nested patterns; ordinary patterns retain semantics. The original rangeLimit/output-expansion behavior is not replaced and is not a promise to solve unrelated glob/regex/expansion advisories. Next's delivered compiled picomatch is a distinct implementation, not this patched braces source; the finding's all-caller review and final bundle remain required.

Install exclusively under the reserved stack with no live service loading modules. Applying is resumable from known pre/postimages. Unknown/corrupted files fail before writes; no original files are deleted. Native/runtime installs and final CI/AFTER are still required. Upstream3.0.3 remains in the affected range even with this downstream control; never dismiss or label it an upstream version fix.

V2 preserves flattened parent links in unmatched literal groups; differential corpus108patterns/432operations identical to upstream. Source/SRI, negative native-stack repro and all previous attempts are retained by BO01.
