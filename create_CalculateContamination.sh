#
# define env variables for GENEPATTERN_USERNAME and GENEPATTERN_PASSWORD
# export GENEPATTERN_USERNAME=ted
# export GENEPATTERN_PASSWORD=
#

python generate-module.py --name gatk.CalculateContamination --instructions "Make sure it can handle both tumor-only mode and matched normal mode.  Optional tool arguments, optional Common arguments and Advanced arguments should not be GeneParameters but --arguments_file should be so that the additional arguments can be passed in.  "  --description "Given pileup data from GetPileupSummaries, calculates the fraction of reads coming from cross-sample contamination.."  --language Python --documentation-url https://gatk.broadinstitute.org/hc/en-us/articles/360036888972-CalculateContamination  --repository-url https://github.com/broadinstitute/gatk --base-image "broadinstitute/gatk:4.1.4.1"  --gp-user $GENEPATTERN_USERNAME --gp-password $GENEPATTERN_PASSWORD \
  --data /Users/liefeld/Desktop/gatk/normal_pileup_summary_table /Users/liefeld/Desktop/gatk/tumor_pileup_summary_table



