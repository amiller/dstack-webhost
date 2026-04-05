/** Combines members of an intersection into a readable type. */
export type Compute<type> = { [key in keyof type]: type[key] } & unknown

/** Collects all keys across every member of a union. */
export type KeyofUnion<type> = type extends type ? keyof type : never

/** Creates a mutually exclusive union where each variant's missing keys are `?: undefined`. */
export type OneOf<
  union extends object,
  fallback extends object | undefined = undefined,
  keys extends KeyofUnion<union> = KeyofUnion<union>,
> = union extends infer item
  ? Compute<
      item & {
        [key in Exclude<keys, keyof item>]?: fallback extends object
          ? key extends keyof fallback
            ? fallback[key]
            : undefined
          : undefined
      }
    >
  : never
