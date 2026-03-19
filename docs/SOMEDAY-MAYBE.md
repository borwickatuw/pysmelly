## `parallel-implementations`

Functions with the same name/signature in different files, or if/else branches that both produce the same type. Hard to detect generically — needs a clearer scope before attempting.

## `boolean-parameter-smell`

Functions with boolean parameters where the first statement is `if flag:` — suggests the function should be two functions. Likely noisy in practice (many legitimate uses of boolean params).

## `stale-comments`

Comments referencing function/variable names that no longer exist in the codebase. Interesting idea but fragile — comments aren't structured, and name matching would produce false positives on partial matches, English words, etc.

## `remainder-flags`

Detect argparse patterns where REMAINDER is used alongside flags that will be swallowed. Very niche — only relevant to CLI-heavy codebases using argparse.REMAINDER.

## look for `dict-builder` function

  As for what pysmelly could detect: this is a "dict-builder function" smell — a function whose primary job is           
  conditionally assembling a dict through mutation. The signal would be: function has N if blocks that each do dict[key] 
  = ... or dict.update(...) on the same variable, with the dict passed to a single API call at the end. That's a pattern 
  that correlates strongly with "hard to read" and "should be decomposed."                                             
## look for 3rd party opportunities

 A few approaches, from simple to ambitious:

  1. Pattern signatures (simplest). pysmelly already does AST analysis. It could match known anti-patterns:              
   
  - argparse.ArgumentParser() appearing in 5+ files → "CLI framework (Click, Typer)"                                     
  - urllib.request or http.client usage → "HTTP client (requests, httpx)"                                              
  - Dataclass with @classmethod + data.get("key", default) pattern → "dict-to-dataclass (dacite, cattrs, Pydantic)"      
  - json.loads() + manual key extraction into typed objects → "validation/parsing (Pydantic)"                            
  - subprocess.run() + stdout parsing → "shell commands (sh, plumbum)"                                                   
  - re.compile() on the same pattern in multiple files → possible shared utility, or a library                           
  - os.path manipulation across many files when pathlib is also used → "inconsistent, pick one"                          
  - datetime.strptime / manual timezone handling → "datetime parsing (python-dateutil, arrow)"                           
                                                                                                                         
  This is a static list — maintainable, auditable, no magic.                                                             
                                                                                                                         
  2. Frequency-based detection. Rather than hardcoding patterns, detect repetition of stdlib usage patterns and flag when
   the repetition crosses a threshold. "You call boto3.client() with error handling in 15 places" — pysmelly doesn't need
   to know which library helps, just that the repetition suggests a wrapper or library would help. Then a lookup table   
  maps boto3.client + try/except ClientError → "consider a shared client factory or aws-error-utils."                  

  3. Community-contributed pattern catalog. A TOML/JSON file that maps AST patterns to library suggestions. Users could  
  contribute patterns, and project maintainers could add their own. pysmelly ships with a default catalog but it's
  extensible:                                                                                                            
                                                                                                                       
  [patterns.cli-framework]
  signal = "argparse.ArgumentParser in 3+ files"                                                                         
  suggest = ["Click", "Typer"]                                                                                           
                                                                                                                         
  [patterns.http-client]                                                                                                 
  signal = "urllib.request or http.client usage"                                                                       
  suggest = ["requests", "httpx"]
                                                                                                                         
  I think #1 with a path toward #3 is the right approach. Start with a hardcoded list of 10-15 well-known patterns — the 
  ones where there's near-universal consensus that a library is better (requests over urllib, for example). Ship it as an
   opt-in check. Then open up the pattern format so others can contribute.                                               
                                                                                                                       
  The key constraint: pysmelly is zero-dependency. It shouldn't need to install or inspect the libraries it recommends — 
  it just recognizes the stdlib patterns they replace.
