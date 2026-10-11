#include <stdio.h>
#include "util.h"

#define SQUARE(x) ((x) * (x))
#define LIMIT 10

typedef struct { int a; int b; } Pair;
struct Node { struct Node *next; int value; };
static int counter;
const char *NAME = "x";

/* Adds two numbers. */
int add(int a, int b) { return a + b; }
static int *lookup(struct Node *head, int value);
