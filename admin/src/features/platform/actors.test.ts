import { describe, expect, it } from 'vitest';
import { auditParties } from './actors';

const OWNER = '01a1b2c3-0000-7000-8000-0000000054fa';
const OTHER = '01a1b2c3-0000-7000-8000-0000000099ff';
const emails = new Map([
  [OWNER, 'owner@example.com'],
  [OTHER, 'ops@example.com'],
]);
const nameOf = (id: string) => emails.get(id);

describe('auditParties', () => {
  it('names the actor and a user subject by email', () => {
    const row = {
      actor_id: OTHER,
      actor_type: 'user',
      resource_type: 'user',
      resource_id: OWNER,
    };
    expect(auditParties(row, nameOf, 'someone-else')).toEqual({
      actor: 'ops@example.com',
      subject: 'owner@example.com',
    });
  });

  it('says You for the signed-in operator and keeps ids for unknown users', () => {
    const unknown = '01a1b2c3-0000-7000-8000-00000000dead';
    const row = {
      actor_id: OWNER,
      actor_type: 'user',
      resource_type: 'user',
      resource_id: unknown,
    };
    const parties = auditParties(row, nameOf, OWNER);
    expect(parties.actor).toBe('You');
    expect(parties.subject).toBe('01a1…dead');
  });

  it('keeps the id of a subject that is not a user, and the actor type without an actor', () => {
    const row = {
      actor_id: null,
      actor_type: 'operator',
      resource_type: 'role',
      resource_id: OWNER,
    };
    expect(auditParties(row, nameOf, OWNER)).toEqual({ actor: 'operator', subject: '01a1…54fa' });
  });
});
