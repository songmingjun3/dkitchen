/**
 * The dkitchen section on this bundle's page in Plugins.
 *
 * It renders into `plugins.detail.section`, the slot a plugin uses for something to say
 * about a bundle it does not own, rather than `plugins.bundle.config`, which is a settings
 * form: nothing here is editable. Installing the Python package needs pipx, logging in to a
 * provider needs a browser, and creating the kitchen profile needs the harness's own
 * launcher, so a button could only fail. The section reports each of those facts and shows
 * the exact command that supplies it, in a field the reader can select and copy.
 *
 * The facts come from the page's `subject`, not from a Remote call: `subject.pkg` carries
 * the bundle name, version, and installed/enabled state the Host already read, which is
 * what this has to say without reaching across the wire for it.
 */
window.__ModuleLoader__.load({
  id: 'dsh-dkitchen',
  factory(require) {
    const React = require('react');
    const h = React.createElement;

    const styles = {
      table: {
        display: 'grid',
        gridTemplateColumns: 'max-content 1fr',
        columnGap: 16,
        rowGap: 8,
        alignItems: 'baseline',
        margin: 0,
      },
      label: { opacity: 0.72 },
      command: {
        display: 'block',
        marginTop: 4,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: '0.92em',
        opacity: 0.9,
        userSelect: 'all',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
      },
      ok: { opacity: 0.72 },
      missing: { fontWeight: 600 },
      note: { opacity: 0.72, marginTop: 12, marginBottom: 0 },
    };

    /** One fact: what it is, its state, and the command that fixes it when it is missing. */
    function Row({ label, value, command, ok }) {
      return [
        h('dt', { key: 'l', style: styles.label }, label),
        h('dd', { key: 'v', style: { margin: 0 } },
          h('span', { style: ok ? styles.ok : styles.missing }, value),
          command ? h('code', { style: styles.command }, command) : null),
      ];
    }

    function Section({ pkg, t }) {
      const installed = pkg && pkg.installed;
      const enabled = pkg && pkg.enabled;
      return h('div', null,
        h('dl', { style: styles.table },
          h(Row, {
            label: t('labelBundle'),
            value: installed ? pkg.version : t('notInstalled'),
            command: installed ? null : 'ding plugin install',
            ok: Boolean(installed),
          }),
          h(Row, {
            label: t('labelEnabled'),
            value: enabled ? t('yes') : t('no'),
            command: enabled ? null : 'ding plugin install',
            ok: Boolean(enabled),
          })),
        h('p', { style: styles.note }, t('setupNote')),
        h('code', { style: styles.command }, 'pipx install dkitchen\nding plugin install'));
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('plugins.detail.section', () => ctx.slots.register({
          name: 'plugins.detail.section',
          id: 'dkitchen-setup',
          order: 20,
          locale: 'dsh-dkitchen',
        }, ({ t, subject }) => {
          if (!subject || subject.kind !== 'bundle' || subject.pkg.name !== 'dsh-dkitchen') {
            return null;
          }
          return h(Section, { pkg: subject.pkg, t });
        }));
      },
    };
  },
});
