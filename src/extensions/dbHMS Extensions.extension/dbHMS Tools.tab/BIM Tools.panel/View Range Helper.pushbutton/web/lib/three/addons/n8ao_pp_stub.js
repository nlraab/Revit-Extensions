// Stub for the optional 'postprocessing' (pmndrs) peer dependency that N8AO.js
// imports for its N8AOPostPass variant. We only use N8AOPass (the vanilla three.js
// EffectComposer pass), so the pmndrs Pass is never instantiated -- this dummy just
// satisfies the import so the module loads offline.
export class Pass {}
