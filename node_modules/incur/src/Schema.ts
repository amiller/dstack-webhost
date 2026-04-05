import { z } from 'zod'

/** Converts a Zod schema to a JSON Schema object. Strips the `$schema` meta-property. */
export function toJsonSchema(schema: z.ZodType): Record<string, unknown> {
  const result = z.toJSONSchema(schema) as Record<string, unknown>
  delete result.$schema
  return result
}
