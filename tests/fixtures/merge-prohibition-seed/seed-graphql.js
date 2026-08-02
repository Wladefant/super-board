// mechanism 3: GraphQL merge mutations
const q1 = 'mutation { mergePullRequest(input: {}) { clientMutationId } }'
const q2 = 'mutation { enablePullRequestAutoMerge(input: {}) { clientMutationId } }'
