# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Nonblocking queue cleanup; never cancels workers or removes game files."""
import copy
import queue
import threading
from contextlib import nullcontext
from .core import Studio, choose_elf

ACTIVE = frozenset(('building', 'analysing', 'analyzing'))

def collect_completed(reader, projects, progress=lambda text: None):
    ids, errors, compiled_inputs = [], [], set()
    # Already removed compiled projects still prove that an EBOOT alias of the
    # same input should not remain queued. Output ownership stays unchanged.
    todo = [p for p in projects if p.get('status') not in ACTIVE]
    for index, project in enumerate(todo, 1):
        progress(f"Checking completed games {index}/{len(todo)}: {project.get('name', 'game')}")
        try:
            candidates = reader.list_test_candidates(project)
            if any(c.get('valid') and c.get('completed') for c in candidates):
                if not project.get('queue_removed'):
                    ids.append(project['id'])
                try:
                    compiled_inputs.add(str(choose_elf(project['source'], project.get('elf','')).resolve()).casefold())
                except (OSError, ValueError, KeyError):
                    pass
        except Exception as error:
            errors.append((project['id'], str(error)))
    for project in todo:
        if project.get('queue_removed') or project['id'] in ids:
            continue
        try:
            source = str(choose_elf(project['source'], project.get('elf','')).resolve()).casefold()
            if source in compiled_inputs:
                ids.append(project['id'])
        except (OSError, ValueError, KeyError):
            pass
    return ids, errors

def _feedback(app, text):
    """Show cleanup progress on the queue tab where its button lives."""
    if not app.__dict__.get('_compiled_queue_cleanup_feedback') and 'tree' in app.__dict__:
        import tkinter as tk
        from tkinter import ttk
        value = tk.StringVar(master=app,value=text)
        label = ttk.Label(app.tree.master,textvariable=value,wraplength=1000)
        label.pack(before=app.tree,fill='x',pady=(3,5))
        app._compiled_queue_cleanup_feedback = value
    value = app.__dict__.get('_compiled_queue_cleanup_feedback')
    if value is not None:
        value.set(text)
    app.compiled_status.set(text)
    if not app.busy:
        app.status.set(text)

def apply_removals(studio, identifiers):
    """Commit visibility flags to current state, never a stale queue snapshot."""
    wanted = set(identifiers)
    lock = studio.state_lock if hasattr(studio, 'state_lock') else nullcontext()
    with lock:
        changed = []
        for p in studio.state['projects']:
            if p['id'] in wanted and not p.get('queue_removed') and p.get('status') not in ACTIVE:
                changed.append((p, 'queue_removed' in p, p.get('queue_removed')))
                p['queue_removed'] = True
        try:
            if changed:
                studio.save()
        except Exception:
            for p, existed, previous in changed:
                if existed:
                    p['queue_removed'] = previous
                else:
                    p.pop('queue_removed', None)
            raise
    return len(changed)

def remove_compiled_async(app):
    """Start hashing off Tk; the after callback alone commits UI/state changes."""
    if getattr(app, '_compiled_queue_cleanup_busy', False):
        _feedback(app,'Completed-game queue cleanup is already checking builds.')
        return
    # Quick pass: games already marked compiled with an existing exe are
    # removed and the list redrawn immediately; the slow verification
    # below only handles whatever is left.
    try:
        from pathlib import Path
        quick = [p['id'] for p in app.studio.state['projects']
                 if not p.get('queue_removed') and str(p.get('status', '')).startswith('compiled')
                 and p.get('native_exe') and Path(p['native_exe']).is_file()]
        if quick:
            count = apply_removals(app.studio, quick)
            app.refresh()
            _feedback(app, f'Removed {count} compiled game(s) from the queue; checking the rest in the background.')
    except Exception as error:
        app.events.put(('log', 'Quick queue cleanup failed: ' + str(error)))
    with app.studio.state_lock if hasattr(app.studio, 'state_lock') else nullcontext():
        snapshot = copy.deepcopy(app.studio.state)
    inbox = queue.Queue()
    app._compiled_queue_cleanup_busy = True
    _feedback(app,'Checking completed games in the background; the active build continues.')

    def work():
        try:
            reader = Studio.read_only_snapshot(app.studio.home)
            reader.state = snapshot
            ids, errors = collect_completed(reader, snapshot['projects'], lambda text: inbox.put(('progress', text)))
            inbox.put(('ready', (ids, errors)))
        except Exception as error:
            inbox.put(('error', str(error)))

    def drain():
        finished = False
        while True:
            try:
                kind, value = inbox.get_nowait()
            except queue.Empty:
                break
            if kind == 'progress':
                _feedback(app,value)
            elif kind == 'error':
                _feedback(app,'Queue cleanup failed: ' + value)
                app.events.put(('log', 'Completed-game queue cleanup failed: ' + value))
                finished = True
            elif kind == 'ready':
                ids, errors = value
                try:
                    count = apply_removals(app.studio, ids)
                    app.refresh()
                    text = f'Removed {count} compiled game(s) from future queue runs. Builds retained in Compiled / built games.'
                    if app.busy:
                        text += ' The current build queue continues unchanged.'
                    if errors:
                        text += f' {len(errors)} game(s) could not be checked; see the log.'
                    _feedback(app,text)
                    app.events.put(('log', text))
                    for project_id, error in errors:
                        app.events.put(('log', f'Queue cleanup retained {project_id}: {error}'))
                except Exception as error:
                    _feedback(app,'Queue cleanup could not be saved: ' + str(error))
                    app.events.put(('log', 'Queue cleanup could not be saved: ' + str(error)))
                finished = True
        if finished:
            app._compiled_queue_cleanup_busy = False
        else:
            app.after(100, drain)

    app._compiled_queue_cleanup_worker = threading.Thread(target=work, daemon=True, name='PS3 compiled queue cleanup')
    app._compiled_queue_cleanup_worker.start()
    app.after(100, drain)
