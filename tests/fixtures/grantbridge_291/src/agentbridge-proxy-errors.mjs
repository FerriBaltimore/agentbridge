// Errors for the dependency-free AgentBridge proxy OAuth transport.
export class GrantBridgeError extends Error {
  constructor(code, message, status = 400) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

export function requireThat(condition, code, message, status = 400) {
  if (!condition) throw new GrantBridgeError(code, message, status);
}
