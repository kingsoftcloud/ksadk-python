import { useEffect, useRef, useCallback } from 'react';
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection, rectangularSelection, crosshairCursor, dropCursor } from '@codemirror/view';
import { EditorState, Compartment } from '@codemirror/state';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { syntaxHighlighting, defaultHighlightStyle, bracketMatching, foldGutter, indentOnInput, foldKeymap, LanguageDescription } from '@codemirror/language';
import { oneDark } from '@codemirror/theme-one-dark';
import { searchKeymap, highlightSelectionMatches } from '@codemirror/search';
import { autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete';
import { languages } from '@codemirror/language-data';

type FileEditorProps = {
  content: string;
  path: string;
  readOnly?: boolean;
  onSave?: (content: string) => void;
  onDirtyChange?: (dirty: boolean) => void;
  onContentChange?: (content: string) => void;
  getContentRef?: React.MutableRefObject<(() => string) | null>;
};

function fileExtension(path: string): string {
  const fileName = String(path || '').split('/').pop() || '';
  const dotIndex = fileName.lastIndexOf('.');
  return dotIndex >= 0 ? fileName.slice(dotIndex) : '';
}

function findLanguageDescription(ext: string): LanguageDescription | null {
  return LanguageDescription.matchFilename(languages, `file${ext}`);
}

export function FileEditor({ content, path, readOnly = false, onSave, onDirtyChange, onContentChange, getContentRef }: FileEditorProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const langCompartmentRef = useRef(new Compartment());
  const darkCompartmentRef = useRef(new Compartment());
  const readOnlyCompartmentRef = useRef(new Compartment());
  const dirtyRef = useRef(false);
  const onSaveRef = useRef(onSave);
  const onDirtyChangeRef = useRef(onDirtyChange);
  const onContentChangeRef = useRef(onContentChange);

  useEffect(() => {
    onSaveRef.current = onSave;
    onDirtyChangeRef.current = onDirtyChange;
    onContentChangeRef.current = onContentChange;
  }, [onSave, onDirtyChange, onContentChange]);

  const setDirty = useCallback((value: boolean) => {
    if (dirtyRef.current !== value) {
      dirtyRef.current = value;
      onDirtyChangeRef.current?.(value);
    }
  }, []);

  // Create editor once
  useEffect(() => {
    if (!containerRef.current) return;

    const isDark = document.documentElement.classList.contains('dark');

    const saveKeymap = keymap.of([{
      key: 'Mod-s',
      run: (view) => {
        const currentContent = view.state.doc.toString();
        onSaveRef.current?.(currentContent);
        setDirty(false);
        return true;
      },
      preventDefault: true,
    }]);

    const startState = EditorState.create({
      doc: content,
      extensions: [
        lineNumbers(),
        highlightActiveLineGutter(),
        history(),
        foldGutter(),
        drawSelection(),
        dropCursor(),
        EditorState.allowMultipleSelections.of(true),
        indentOnInput(),
        syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
        bracketMatching(),
        closeBrackets(),
        autocompletion(),
        rectangularSelection(),
        crosshairCursor(),
        highlightActiveLine(),
        highlightSelectionMatches(),
        keymap.of([
          ...closeBracketsKeymap,
          ...defaultKeymap,
          ...searchKeymap,
          ...historyKeymap,
          ...foldKeymap,
          ...completionKeymap,
          indentWithTab,
        ]),
        saveKeymap,
        EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            setDirty(true);
            onContentChangeRef.current?.(update.state.doc.toString());
          }
        }),
        readOnlyCompartmentRef.current.of(EditorState.readOnly.of(readOnly)),
        langCompartmentRef.current.of([]),
        darkCompartmentRef.current.of(isDark ? oneDark : []),
        EditorView.theme({
          '&': { height: '100%' },
          '.cm-scroller': { overflow: 'auto' },
        }),
      ],
    });

    const view = new EditorView({
      state: startState,
      parent: containerRef.current,
    });

    viewRef.current = view;

    // Expose getter for parent components to retrieve current content
    if (getContentRef) {
      getContentRef.current = () => view.state.doc.toString();
    }

    // Load language dynamically
    const desc = findLanguageDescription(fileExtension(path));
    if (desc) {
      desc.load().then((support) => {
        if (viewRef.current) {
          viewRef.current.dispatch({
            effects: langCompartmentRef.current.reconfigure(support),
          });
        }
      }).catch(() => {
        // language load failed, continue without highlighting
      });
    }

    return () => {
      view.destroy();
      viewRef.current = null;
      if (getContentRef) {
        getContentRef.current = null;
      }
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Update content when prop changes (e.g., file switch)
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const currentDoc = view.state.doc.toString();
    if (currentDoc === content) return;
    view.dispatch({
      changes: { from: 0, to: currentDoc.length, insert: content },
    });
    setDirty(false);
  }, [content, setDirty]);

  // Update language when path changes
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const desc = findLanguageDescription(fileExtension(path));
    if (desc) {
      desc.load().then((support) => {
        if (viewRef.current) {
          viewRef.current.dispatch({
            effects: langCompartmentRef.current.reconfigure(support),
          });
        }
      }).catch(() => {
        // language load failed, continue without highlighting
      });
    } else {
      view.dispatch({
        effects: langCompartmentRef.current.reconfigure([]),
      });
    }
  }, [path]);

  // Update read-only state
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: readOnlyCompartmentRef.current.reconfigure(EditorState.readOnly.of(readOnly)),
    });
  }, [readOnly]);

  // Watch for dark mode changes
  useEffect(() => {
    const observer = new MutationObserver(() => {
      const view = viewRef.current;
      if (!view) return;
      const isDark = document.documentElement.classList.contains('dark');
      view.dispatch({
        effects: darkCompartmentRef.current.reconfigure(isDark ? oneDark : []),
      });
    });

    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class'],
    });

    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={containerRef}
      className="h-full w-full overflow-hidden [&_.cm-editor]:h-full [&_.cm-scroller]:auto"
    />
  );
}
