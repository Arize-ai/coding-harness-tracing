/**
 * Minimal manual mock for the `vscode` module.
 * Provides stubs sufficient to import and test extension.ts under Jest.
 */

export const commands = {
  registerCommand: jest.fn((_id: string, _cb: (...args: unknown[]) => unknown) => ({
    dispose: jest.fn(),
  })),
  executeCommand: jest.fn((_id: string, ..._args: unknown[]) => Promise.resolve()),
};

export const ViewColumn = { One: 1, Two: 2, Three: 3 };

export enum StatusBarAlignment {
  Left = 1,
  Right = 2,
}

export enum ProgressLocation {
  SourceControl = 1,
  Window = 10,
  Notification = 15,
}

export enum QuickPickItemKind {
  Separator = -1,
  Default = 0,
}

export const window = {
  showInformationMessage: jest.fn((_msg: string, ..._rest: unknown[]) => Promise.resolve(undefined)),
  showWarningMessage: jest.fn((_msg: string, ..._rest: unknown[]) => Promise.resolve(undefined)),
  showQuickPick: jest.fn((_items: unknown[], _opts?: unknown) => Promise.resolve(undefined)),
  showOpenDialog: jest.fn((_opts?: unknown) => Promise.resolve(undefined)),
  createOutputChannel: jest.fn((_name: string) => ({
    appendLine: jest.fn(),
    append: jest.fn(),
    clear: jest.fn(),
    show: jest.fn(),
    hide: jest.fn(),
    dispose: jest.fn(),
  })),
  withProgress: jest.fn((_options: unknown, task: (progress: unknown, token: { onCancellationRequested: (cb: () => void) => { dispose: () => void } }) => Promise<unknown>) => {
    const token = { onCancellationRequested: jest.fn(() => ({ dispose: jest.fn() })) };
    return task({}, token);
  }),
  createStatusBarItem: jest.fn((_alignment?: number, _priority?: number) => ({
    text: "",
    tooltip: "",
    command: undefined as string | undefined,
    show: jest.fn(),
    hide: jest.fn(),
    dispose: jest.fn(),
  })),
  createWebviewPanel: jest.fn(
    (_viewType: string, _title: string, _column: number, _opts?: unknown) => {
      const _listeners: Array<(e: unknown) => void> = [];
      const _disposeListeners: Array<() => void> = [];
      return {
        webview: {
          html: "",
          cspSource: "https://test.csp",
          asWebviewUri: jest.fn((uri: { path: string }) => uri),
          onDidReceiveMessage: jest.fn((cb: (e: unknown) => void) => {
            _listeners.push(cb);
            return { dispose: jest.fn() };
          }),
          postMessage: jest.fn(),
          /** Test helper: simulate a message from the webview. */
          _simulateMessage(msg: unknown) {
            for (const cb of _listeners) cb(msg);
          },
        },
        onDidDispose: jest.fn((cb: () => void) => {
          _disposeListeners.push(cb);
          return { dispose: jest.fn() };
        }),
        reveal: jest.fn(),
        dispose: jest.fn(() => {
          for (const cb of _disposeListeners) cb();
        }),
        /** Test helper: simulate the panel being closed by the user. */
        _simulateDispose() {
          for (const cb of _disposeListeners) cb();
        },
      };
    },
  ),
  registerWebviewViewProvider: jest.fn((_id: string, _provider: unknown) => ({
    dispose: jest.fn(),
  })),
};

interface MockUri {
  scheme: string;
  path: string;
  toString: () => string;
}

function makeUri(scheme: string, path: string): MockUri {
  return {
    scheme,
    path,
    toString: () => `${scheme}://${path}`,
  };
}

export const Uri = {
  file: jest.fn((path: string) => makeUri("file", path)),
  joinPath: jest.fn((base: { path: string }, ...segments: string[]) =>
    makeUri("file", base.path + "/" + segments.join("/"))),
};

export const workspace = {
  workspaceFolders: undefined as Array<{ uri: { fsPath: string } }> | undefined,
};

export class EventEmitter {
  private _listeners: Array<(e: unknown) => void> = [];

  event = jest.fn((listener: (e: unknown) => void) => {
    this._listeners.push(listener);
    return { dispose: jest.fn(() => {
      this._listeners = this._listeners.filter((l) => l !== listener);
    }) };
  });

  fire = jest.fn((data: unknown) => {
    for (const listener of this._listeners) {
      listener(data);
    }
  });

  dispose = jest.fn(() => {
    this._listeners = [];
  });
}

export class Disposable {
  static from(..._disposables: Array<{ dispose: () => void }>): Disposable {
    return new Disposable();
  }
  dispose = jest.fn();
}
